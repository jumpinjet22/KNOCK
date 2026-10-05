#!/usr/bin/env bash
# Overnight KNOCK LoRA fine-tune, run unattended via crontab (see
# `crontab -l` for the scheduled invocation). Not part of CI -- a local,
# hands-on dev tool like the other scripts/ files.
#
# Trains QLoRA adapters on two sizes of qwen3.5's real upstream
# (unsloth/Qwen3.5-9B, matching the current production model, and
# unsloth/Qwen3.5-0.8B, the smallest dense variant) using tonight's
# reviewed synthetic training data (scripts/unsloth_training_data.generated.jsonl,
# from scripts/export_for_unsloth.py). The end goal is distillation: see
# whether the much smaller 0.8B model, fine-tuned on this narrow task,
# can match the 9B production model's quality -- if so, production can
# move to a far cheaper/faster model.
#
# Only the 0.8B run attempts a GGUF export (so the result is actually
# Ollama-loadable for an apples-to-apples comparison against production).
# The 9B run deliberately skips it -- GGUF export needs a full-precision
# merge of the adapter into the base model before llama.cpp can quantize
# it, which takes ~20GB regardless of how the base was loaded, and this
# card only has 15.5GB. Confirmed via a live OOM during a smoke test, with
# and without --load-in-4bit on the export step. The 0.8B model's full
# fp16 size is well under that ceiling, so its export should just work.
# Verification against the 9B adapter instead runs directly against the
# 4-bit base + adapter via `unsloth chat`/`unsloth inference`, which never
# needs that merge.
#
# `--output-dir`/export paths must live under
# /home/jon/.unsloth/studio/outputs -- the CLI sandboxes output to that
# root and refuses any path outside it (confirmed via a live smoke test).
set -uo pipefail

REPO_DIR="/home/jon/Knock/KNOCK"
UNSLOTH="/home/jon/.local/bin/unsloth"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

cd "$REPO_DIR" || { echo "repo dir missing"; exit 127; }

# Re-export fresh in case more reviews were approved since this script was
# written -- cheap (a few seconds), and keeps the training data current.
.venv/bin/python scripts/export_for_unsloth.py --output scripts/unsloth_training_data.generated.jsonl
export_exit=$?
if [ $export_exit -ne 0 ]; then
    STATUS_DIR="/home/jon/unsloth_runs/knock-export-failed-${TIMESTAMP}"
    mkdir -p "$STATUS_DIR"
    echo "export_exit=${export_exit}" > "${STATUS_DIR}/DONE"
    echo "data export failed, aborting both runs"
    exit $export_exit
fi

run_training() {
    local size="$1"
    local base_model="$2"
    local do_gguf="$3"

    local run_name="knock-lora-${size}-${TIMESTAMP}"
    local out_dir="/home/jon/.unsloth/studio/outputs/${run_name}"
    local status_dir="/home/jon/unsloth_runs/${run_name}"
    mkdir -p "$status_dir"

    {
        echo "=== $(date) starting ${run_name} (${base_model}) ==="

        "$UNSLOTH" train \
            --model "$base_model" \
            --local-dataset "${REPO_DIR}/scripts/unsloth_training_data.generated.jsonl" \
            --format-type chatml \
            --load-in-4bit \
            --output-dir "$out_dir"
        local train_exit=$?

        local gguf_dir=""
        local gguf_exit=""
        if [ "$train_exit" -eq 0 ] && [ "$do_gguf" = "yes" ]; then
            gguf_dir="/home/jon/.unsloth/studio/outputs/${run_name}-gguf"
            "$UNSLOTH" export "$out_dir" "$gguf_dir" --format gguf --quantization q4_k_m --load-in-4bit
            gguf_exit=$?
        fi

        {
            echo "train_exit=${train_exit}"
            echo "output_dir=${out_dir}"
            echo "base_model=${base_model}"
            echo "size=${size}"
            [ -n "$gguf_dir" ] && echo "gguf_dir=${gguf_dir}"
            [ -n "$gguf_exit" ] && echo "gguf_exit=${gguf_exit}"
            echo "finished_at=$(date -Iseconds)"
        } > "${status_dir}/DONE"

        echo "=== $(date) finished ${run_name} (train=${train_exit}) ==="
    } > >(tee -a "${status_dir}/run.log") 2>&1
}

run_training "9b" "unsloth/Qwen3.5-9B" "no"
run_training "0.8b" "unsloth/Qwen3.5-0.8B" "yes"
