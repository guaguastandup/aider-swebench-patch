#!/bin/bash
# SWE-bench Task Runner Script
# This script demonstrates how to run individual tasks with different models

# Supported models (examples):
# OpenAI: openai/gpt-4o-2024-05-13, openai/gpt-4o-mini-2024-07-18
# DeepSeek: deepseek/deepseek-reasoner

# Available tasks:
# - astropy__astropy-12907
# - astropy__astropy-13977
# - django__django-11265
# - django__django-11299
# - sympy__sympy-13615
# - sympy__sympy-13974
# - sympy__sympy-13757

# Activate conda environment
conda activate aider-swe2

# Example 1: Run with DeepSeek (automatic trace collection)
export TASK="sympy__sympy-13757"
export DEEPSEEK_API_KEY=sk-your-api-key-here
export MODEL=deepseek/deepseek-reasoner
python run_swe_task.py --task "$TASK" --model "$MODEL" --force-rerun

# Example 2: Run with OpenAI (manual trace export required)
# Note: For OpenAI models, you need to:
# 1. Run the task first
# 2. Export the trace from https://platform.openai.com/logs using the browser plugin
# 3. Place the exported trace in the appropriate directory
# export TASK="astropy__astropy-12907"
# export OPENAI_API_KEY=sk-proj-your-api-key-here
# export MODEL=openai/gpt-4o-mini-2024-07-18
# python run_swe_task.py --task "$TASK" --model "$MODEL" --force-rerun

# Run all tasks in parallel (optional)
# python run_swe_task.py --task all --model "$MODEL" --parallel 4

# List all available tasks (optional)
# python run_swe_task.py --list-tasks
