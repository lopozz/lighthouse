# Inspired from https://github.com/huggingface/optimum/blob/main/tests/benchmark/benchmark_gptq.py

import argparse
import itertools
import gc
import os
import time
import auto_gptq
from utils import *

import numpy as np
import pandas as pd
import torch
from auto_gptq.utils import Perplexity
from memory_tracker import MemoryTracker
from tqdm import tqdm
from transformers import (
    AutoModel,
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    GenerationConfig,
    GPTQConfig,
)

from optimum.exporters import TasksManager

def parse_arguments():
    parser = argparse.ArgumentParser(
        prog='GPTQ benchmarking',
        description='Generate inference stats for GPTQ quantized models.'
    )
    parser.add_argument("--model", type=str, help="Path of Model to benchmark.")
    parser.add_argument("--replica", type=int, default=10, help="Number of repeated experiments.")
    parser.add_argument("--batch-size", action='store',  nargs='+', type=int, default=[1], help="Prompt processing maximum batch size.")
    parser.add_argument("--task", type=str, default=None, help="Task")
    parser.add_argument("--prompt-length", nargs='+', type=int, default=[100], help="Length of the prompt (ratio fro te context window menus new_tokens).")
    parser.add_argument("--new-tokens", nargs='+', type=int, default=[100], help="Length of the generation.")
    parser.add_argument("--prefill", action="store_true", help="For decoder models, benchmark only the prefill step with `prompt_length`.")
    parser.add_argument("--gptq", action="store_true", help="Indicate that the model to benchmark is a GPTQ model.")
    parser.add_argument("--bitsandbytes", action="store_true", help="Indicate that the model uses bitsandbytes through transformers load_in_4bit=True.")
    parser.add_argument("--use-exllama", action="store_true", help="Use Exllama kernel, to rather use the AutoGPTQ CUDA (act-order case) or CUDA-old (no act-order case) kernels.")
    parser.add_argument("--exllama-version", type=int, default=2, help="Use Exllamav2 kernel. Set 1 in order to use exllama kernel")
    parser.add_argument("--generate", action="store_true", help="Calculate the generate speed (prompt processing + token generation)")
    parser.add_argument("--ppl", action="store_true", help="Calculate the perplexity on wikitext2 dataset")
    parser.add_argument("--revision", default='main', help="Revision of the model to benchmark")
    parser.add_argument("--debug", action='store_true', default=False, help="Reslts won't be stored.")
    parser.add_argument("--memo", type=str, default='', help="Description of the experiment.")

    return parser.parse_args()

def load_model(model: str,
               gptq: bool,
               bitsandbytes: bool,
               revision: str,
               use_exllama: bool,
               exllama_version: int):
        
        load_start = time.time_ns()
        
        if gptq:
            quantization_config = GPTQConfig(
                bits=4, use_exllama=use_exllama, exllama_config={"version": exllama_version}
            )
            model = autoclass.from_pretrained(
                model,
                revision=revision,
                quantization_config=quantization_config,
                torch_dtype=torch.float16,
                device_map="auto",
                trust_remote_code=True,
            )
        elif bitsandbytes:
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="fp4", bnb_4bit_compute_dtype=torch.float16
            )
            model = autoclass.from_pretrained(
                model, quantization_config=quantization_config, device_map="auto", torch_dtype=torch.float16, trust_remote_code=True
            )
        else:
            with device:
                model = autoclass.from_pretrained(model, torch_dtype=torch.float16, trust_remote_code=True)
        
        torch.cuda.synchronize()
        
        load_end = time.time_ns()

        load_time = (load_end - load_start) * 1e-9

        return model, load_time

def get_timings(
        model, 
        replica: int,
        input_ids: torch.Tensor,
        masks: torch.Tensor,
        is_decoder: bool,
        generation_config=None):

    assert generation_config.min_new_tokens == generation_config.max_new_tokens

    torch.cuda.synchronize()

    # We need NOT call torch.cuda.empty_cache() here as it appears to negate the warmup.

    latencies = []
    for _ in tqdm(range(replica)):
        start_event = torch.cuda.Event(enable_timing=True)
        end_event = torch.cuda.Event(enable_timing=True)
        torch.cuda.synchronize()
        start_event.record()

        if is_decoder:
            _ = model.generate(input_ids, attention_mask=masks, generation_config=generation_config)
        else:
            _ = model(input_ids, masks)
        end_event.record()
        torch.cuda.synchronize()

        latency_s = start_event.elapsed_time(end_event) / 1000
        print(f"\nLatency (Tk/s): {latency_s / generation_config.min_new_tokens:.3f} s")
        latencies.append(latency_s)

    return np.mean(latencies)

def warmup(
    model,
    input_ids: torch.Tensor,
    masks: torch.Tensor,
    is_decoder: bool,
    new_tokens: int,
    pad_token_id: int):

    print("Warmup...")
    if is_decoder:
        gen_config = GenerationConfig(
            max_new_tokens=new_tokens,
            min_new_tokens=new_tokens,
            use_cache=True,
            pad_token_id=pad_token_id,
            num_beams=1,
            do_sample=False,
            eos_token_id=None,  # This is required for min_new_tokens to actually have an effect.
        )
        model.generation_config.eos_token_id = None  # greedy_search falls back on this eos_token_id that we need to set to None as well for min_new_tokens to have an effect.
        res = model.generate(input_ids, attention_mask=masks, generation_config=gen_config)
        assert res.shape[1] == new_tokens + input_ids.shape[1]
        del res
    else:
        gen_config = None
        _ = model(input_ids, masks)
    torch.cuda.synchronize()

    return gen_config


def benchmark_latency(
    model,
    input_ids: torch.Tensor,
    masks: torch.Tensor,
    replica: int,
    is_decoder: bool,
    new_tokens: int,
    pad_token_id: int,
    memory_tracker: MemoryTracker,
):
    torch.cuda.empty_cache()
    gc.collect()

    gen_config = warmup(
        model,
        input_ids,
        masks,
        is_decoder,
        new_tokens,
        pad_token_id,
    )

    print("Measuring latency...")
    total_time = get_timings(model, replica, input_ids, masks, is_decoder, gen_config)

    return total_time

# change this to also say if OOM 
def benchmark_memory(
    model,
    input_ids: torch.Tensor,
    masks: torch.Tensor,
    replica: int,
    is_decoder: bool,
    new_tokens: int,
    pad_token_id: int,
    memory_tracker: MemoryTracker,
):
    torch.cuda.empty_cache()
    gc.collect()

    print("Measuring peak memory...")
    with memory_tracker.track():
        gen_config = warmup(
            model,
            input_ids,
            masks,
            is_decoder,
            new_tokens,
            pad_token_id,
        )

        if is_decoder:
            _ = model.generate(input_ids, attention_mask=masks, generation_config=gen_config)
        else:
            _ = model(input_ids, masks)

        torch.cuda.synchronize()

    memory_stats = torch.cuda.memory_stats()

    peak_allocated_torch_mb = memory_stats["allocated_bytes.all.peak"] * 1e-6
    peak_reserved_torch_mb = memory_stats["reserved_bytes.all.peak"] * 1e-6

    peak_nvml_mb = memory_tracker.peak_memory

    # I am not sure whether we should substract here `inactive_split_bytes.all.peak` (not sure what it corresponds to, though it can get quite large, in the several GB).
    peak_external_mb = peak_nvml_mb - peak_reserved_torch_mb
    # assert peak_external_mb > 0

    # This formula is to confirm. We measure the actual allocated PyTorch memory, plus the additional non-PyTorch memory (as the CUDA context, CUDA extension device memory). We need to substract the PyTorch peak reserved memory since this one appears in the peak nvidia-smi/nvmlDeviceGetMemoryInfo.

    # NOTE: I verified this is only a ROUGH estimate. It may be better to use PYTORCH_NO_CUDA_MEMORY_CACHING=1 and just nvmlDeviceGetMemoryInfo.
    # We can actually doubt whether it make sense to try to estimate when we would OOM, given that different devices, CUDA version do have
    # a different CUDA context size.
    peak_memory_mb = peak_allocated_torch_mb + peak_external_mb

    print(f"DEBUG: peak allocated torch: {peak_allocated_torch_mb:.2f} MB")
    print(f"DEBUG: peak nvidia-smi/nvml: {peak_nvml_mb:.2f} MB")
    print(f"DEBUG: peak reserved torch: {peak_reserved_torch_mb:.2f} MB")
    print(f"DEBUG: peak external: {peak_external_mb:.2f} MB")
    print(f"DEBUG: global peak: {peak_memory_mb:.2f} MB")

    return peak_memory_mb

if __name__ == '__main__':
    args = parse_arguments()

    RUN_NAME = random_noun_adjective()
    print(RUN_NAME)

    bulb = pd.Dataframe() if not os.path.exists('bulb.csv') else pd.read_csv('bulb.csv')

    if args.prefill:
        print("Running the prefill benchmark: generating only one new token.")
        new_tokens = [1]

    if not torch.cuda.is_available():
        raise ValueError("A cuda device is necessary to benchmark GPTQ.")
    if len(os.environ["CUDA_VISIBLE_DEVICES"].split(",")) != 1:
        raise ValueError(
            "Please set CUDA_VISIBLE_DEVICES variable to a single device index. This benchmark code is not tested for multi-device setup."
        )

    device = torch.device("cuda:0")
    memory_tracker = MemoryTracker()
    model_path = args.model

    #tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision, use_fast=False, trust_remote_code=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model, revision=args.revision, use_fast=True, trust_remote_code=True)

    if not hasattr(tokenizer, "pad_token") or tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if args.task:
        task = args.task
    else:
        task = TasksManager.infer_task_from_model(args.model)

    if task == "text-generation":
        autoclass = AutoModelForCausalLM
    elif task == "text2text-generation":
        autoclass = AutoModelForSeq2SeqLM
    else:
        autoclass = AutoModel

    if task in ["text-generation", "text2text-generation"]:
        is_decoder = True
    else:
        is_decoder = False

    model, load_time = load_model(args.model,
                                  args.gptq,
                                  args.bitsandbytes,
                                  args.revision,
                                  args.use_exllama,
                                  args.exllama_version)
    print(f"Model load time: {load_time:.1f} s")

    act_order = None
    bits = None
    group_size = None
    kernel = None

    if args.gptq:
        quantization_config_dict = model.config.quantization_config.to_dict()
        print(quantization_config_dict)
        act_order = quantization_config_dict["desc_act"]
        bits = quantization_config_dict["bits"]
        group_size = quantization_config_dict["group_size"]
        use_exllama = quantization_config_dict["use_exllama"]
        exllama_version = quantization_config_dict["exllama_config"]["version"]

        if use_exllama:
            if exllama_version == 2:
                kernel = "exllamav2"
            else:
                kernel = "exllama"
        elif act_order:
            kernel = "autotogptq-cuda"
        else:
            kernel = "autogptq-cuda-old"

    uses_gptq = args.gptq
    uses_bitsandbytes = args.bitsandbytes
    print(f"Model uses GPTQ: {uses_gptq}")
    print(f"Model uses bitsandbytes: {uses_bitsandbytes}")
    print(f"Using accelerate hooks: {hasattr(model, '_hf_hook')}")
    print(f"Bits: {bits}")
    print(f"group_size: {group_size}")
    print(f"act_order: {act_order}")
    print(f"kernel: {kernel}")

    model = model.eval()

    file_name = "log_{}".format(args.model.replace("/", "-"))

    if uses_gptq:
        quantization = "gptq"
        file_name = file_name + "_gptq"
    elif uses_bitsandbytes:
        file_name = file_name + "_bnb"
        quantization = "bitsandbytes"
    else:
        file_name = file_name + "_noquant"
        quantization = None

    if args.ppl:
        output_file = open(file_name + "_perplexity.csv", "w")
        header = "quantization, act_order, bits, group_size, kernel, perplexity\n"
        output_file.write(header)
        ppl = Perplexity(model, tokenizer)
        ppl_value = np.mean(ppl.calculate_perplexity())
        line = "{},{},{},{},{},{}\n".format(
            quantization,
            act_order,
            bits,
            group_size,
            kernel,
            f"{ppl_value:.2f}",
        )
        print(header)
        print(line)
        output_file.write(line)
        output_file.close()

    if args.generate:

        param_combinations = list(itertools.product(
            args.batch_size,
            args.prompt_length,
            args.new_tokens
        ))

        # output_file = open(file_name + ".csv", "w")
        # header = "quantization, act_order, bits, group_size, kernel, replica, batch_size, prompt_length, new_tokens, Load time (s), Per-token latency (ms), Throughput (tok/s), Max memory (MB)\n"
        # output_file.write(header)

        latencies = {}
        throughputs = {}
        all_max_mem = {}
        print(
            "WARNING: The reported peak memory is only a rough estimate, and can NOT be precisely relied upon to estimate an OOM limit."
        )

        for params in tqdm(param_combinations, total=len(param_combinations), desc='Running experiments'):
            batch_size, prompt_length, new_tokens = params
            print(f"---- Running: batch_size={batch_size}, prompt_length={prompt_length}, new_tokens={new_tokens}")

            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()

            input_ids = torch.randint(1, model.config.vocab_size - 1, size=(batch_size, prompt_length)).to(device)
            masks = torch.ones(batch_size, prompt_length, dtype=torch.int32).to(device)

            with torch.no_grad():
                max_mem = benchmark_memory(
                    model,
                    input_ids,
                    masks,
                    args.replica,
                    is_decoder,
                    new_tokens,
                    tokenizer.pad_token_id,
                    memory_tracker=memory_tracker,
                )

                mean_latency = benchmark_latency(
                    model,
                    input_ids,
                    masks,
                    args.replica,
                    is_decoder,
                    new_tokens,
                    tokenizer.pad_token_id,
                    memory_tracker=memory_tracker,
                )

            index = (batch_size, prompt_length, new_tokens)

            per_token_latency = mean_latency / (new_tokens)
            latencies[index] = per_token_latency

            throughput = batch_size / (per_token_latency * 1e-3)
            throughputs[index] = throughput
            all_max_mem[index] = max_mem

            print(
                f"Latency per token: {per_token_latency:.3f} s, throughput: {throughput:.3f} tok/s, peak mem: {max_mem:.2f} MB"
            )

            line = "{},{},{},{},{},{},{},{},{},{},{},{},{}\n".format(
                quantization,
                act_order,
                bits,
                group_size,
                kernel,
                args.replica,
                batch_size,
                prompt_length,
                new_tokens,
                f"{load_time:.2f}",
                f"{per_token_latency:.2f}",
                f"{throughput:.2f}",
                f"{max_mem:.2f}",
            )

            experiment = {
                "memo": args.memo,
                "auto_gptq_v": auto_gptq.__version__,
                "Run Name": RUN_NAME,
                "Device": torch.cuda.get_device_name(),
                "VRAM (GB)": round(torch.cuda.get_device_properties('cuda').total_memory / 1024 / 1024 / 1024, 2),
                "RAM (GB)": round(psutil.virtual_memory().total / 1024 / 1024 / 1024, 2),
                "CPU Count": len(psutil.Process().cpu_affinity()),
                "Model": os.path.basename(model_path)+'-'+args.revision,
                #"Model Size (GB)": round(os.path.getsize(model_path) / 1024 / 1024 / 1024, 2),
                "Batch": batch_size,
                "Prompt Length": prompt_length,
                "New Tokens": new_tokens,
                "Load Time (s)": f"{load_time:.2f}",
                "Latency (s)": f"{mean_latency:.2f}",
                "Latency (Tk/s)": f"{per_token_latency:.2f}",
                "Throughput (s/Tk)": f"{throughput:.2f}",
                "Kernel": kernel,
                "Quant. Method": 'gptq',
                "Mem. Usage": f"{max_mem:.2f}"
                }

            if not args.debug:
                bulb = pd.concat([bulb, pd.DataFrame([experiment])], ignore_index=True)

        if not args.debug:
            bulb.to_csv('bulb.csv', index=False)