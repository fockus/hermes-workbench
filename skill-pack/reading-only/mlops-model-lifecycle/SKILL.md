---
name: mlops-model-lifecycle
description: "Use when operating ML/LLM model lifecycle workflows: hub search/download/upload, evaluation harnesses, experiment tracking, local inference, serving, quantization, model surgery, segmentation, and audio models."
version: 1.0.0
author: Hermes Workbench contributors
license: MIT
metadata:
  hermes:
    tags: [mlops, huggingface, evaluation, wandb, llama-cpp, vllm, quantization, sam, audiocraft]
    related_skills: []
---

# MLOps Model Lifecycle

## Overview

This umbrella covers model acquisition, experiments, evaluation, local inference, serving, quantization, model modification, and model-specific workflows across LLM, audio, and vision systems.

## When to Use

- Search, download, or upload models/datasets on Hugging Face.
- Run lm-eval-harness or other benchmark suites.
- Track experiments, artifacts, sweeps, and dashboards in W&B.
- Run GGUF models locally with llama.cpp or serve with vLLM.
- Quantize, optimize, or troubleshoot inference.
- Apply model surgery such as refusal-vector abliteration.
- Use Segment Anything or AudioCraft/MusicGen/AudioGen.

## Lifecycle Branches

### Discover and acquire
Validate licenses, model files, revisions, and disk requirements before downloading.

### Evaluate and track
Pin datasets, prompts, metrics, seeds, and run metadata; log artifacts and outputs for reproducibility.

#### lm-evaluation-harness benchmarking
Use `lm_eval` for standardized academic and release benchmarks such as MMLU, GSM8K, HellaSwag, TruthfulQA, ARC, HumanEval, and MBPP. Select task suites deliberately, record few-shot counts and model args, prefer `--batch_size auto` when safe, and save `--output_path` plus `--log_samples` when diagnosing model behavior. Use the vLLM backend (`--model vllm`) when benchmark throughput matters. Full command banks and task details live in `references/evaluating-llms-harness.md` and `references/lm-evaluation-harness/`.

### Serve and infer
Choose llama.cpp for local GGUF/CPU-GPU mixed inference; vLLM for high-throughput OpenAI-compatible serving.

#### vLLM production serving
Use vLLM for OpenAI-compatible LLM APIs, high request concurrency, continuous batching, PagedAttention KV-cache efficiency, tensor parallelism, and quantized serving (AWQ/GPTQ/FP8). Verify ports, GPU memory, `--max-model-len`, `--gpu-memory-utilization`, metrics, and a real OpenAI SDK request before calling a server ready. Detailed deployment, optimization, quantization, and troubleshooting notes are in `references/serving-llms-vllm.md` and `references/vllm/`.

For a memory service with an independent embedder, pin the serving model and test a synthetic write and retrieval in the intended scope. Container readiness and raw storage readback do not prove that derivation ran.

### Modify and optimize
Quantization and abliteration require before/after evaluation and rollback paths.

### Model-specific workflows
SAM and AudioCraft have modality-specific inputs/outputs; verify generated masks/audio files directly.

#### AudioCraft / MusicGen / AudioGen
Use AudioCraft for text-to-music, text-to-sound, melody-conditioned generation, style conditioning, and EnCodec audio tokenization/compression. Confirm sample rates (`32000` for MusicGen, often `16000` for AudioGen examples), model size, duration, temperature/top-k/top-p/CFG settings, and write generated audio to a real file before reporting success. Detailed recipes are in `references/audiocraft-audio-generation.md` and `references/audiocraft/`.

#### Segment Anything (SAM)
Use SAM for zero-shot image segmentation with point prompts, box prompts, previous-mask refinement, and automatic mask generation. Choose ViT-B/L/H based on VRAM/speed, download the matching checkpoint, compute image embeddings once per image, and verify masks visually or by saved mask artifacts. Detailed predictor, ONNX, batching, and troubleshooting recipes are in `references/segment-anything-model.md` and `references/segment-anything/`.

## Consolidated Reference Packages

The former narrow MLOps skills have been absorbed here as support files:

- AudioCraft: `references/audiocraft-audio-generation.md`, `references/audiocraft/advanced-usage.md`, `references/audiocraft/troubleshooting.md`
- lm-evaluation-harness: `references/evaluating-llms-harness.md`, `references/lm-evaluation-harness/benchmark-guide.md`, `references/lm-evaluation-harness/custom-tasks.md`, `references/lm-evaluation-harness/api-evaluation.md`, `references/lm-evaluation-harness/distributed-eval.md`
- vLLM: `references/serving-llms-vllm.md`, `references/vllm/server-deployment.md`, `references/vllm/optimization.md`, `references/vllm/quantization.md`, `references/vllm/troubleshooting.md`
- Segment Anything: `references/segment-anything-model.md`, `references/segment-anything/advanced-usage.md`, `references/segment-anything/troubleshooting.md`

## Common Pitfalls

1. Model downloads and serving are resource-sensitive; check disk, RAM/VRAM, and ports.
2. Evaluation results are meaningless without pinned versions and configs.
3. Safety/model-surgery changes need explicit before/after verification.

## Verification Checklist

- [ ] Model/data identifiers and revisions recorded.
- [ ] Required hardware/resources checked.
- [ ] Inference/evaluation/output artifacts verified.
