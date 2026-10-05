# tools/finetune — QLoRA fine-tune scaffolding

Dry-run the training pipeline on **Qwen2.5-VL-7B** on the RTX 4080, then scale the
*same* scripts to **Qwen3-VL-32B** on a rented H100.

## Pipeline
```
build_starter_dataset.py   real corpus pages -> render + EXACT production prompt
                           (+ optional draft from the served base model)
        |                  -> data/drafts.jsonl   (correctable)
   [human corrects `target`, sets reviewed=true]
        v
finalize_dataset.py        reviewed drafts -> Unsloth conversation train/val JSONL
        |                  -> data/train.jsonl, data/val.jsonl
        v
train_qlora_vision.py      Unsloth QLoRA (7B on 4080 4-bit; 32B on H100) -> LoRA adapter
        v
eval_adapter.py            quick base-vs-adapter valid-JSON smoke (real gates:
                           merge+serve, run tools/run_vision_eval.py)
```

## Key rules
- **Data is the gate.** Nothing trains without corrected `target`s. Dry-run needs
  only 20–50; the real model needs 300–1,000 page-task examples.
- **Never train on `tools/corpus_annotations/v1`** — that is the eval holdout.
- **One GPU, one job** — stop Ollama on the box while training (16 GB).
- Prompts come from `vision_prompts.py`, so a tuned adapter drops into the
  production `pdf_vision` provider path after merge (no prompt drift).
- `--use-drafts-as-target` (finalize) is a **plumbing smoke only** — trains on the
  base model's own output; never ship it.
