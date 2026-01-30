## ⚙️ Setup & Execution Guide

This section details how to reproduce the traces used in this analysis using [aider-swe-bench](https://github.com/Aider-AI/aider-swe-bench).

### 1. Environment Preparation

Before running tasks, you must configure the python environment and apply specific patches to Aider.

**Create Environment**:
Clone [aider-swebench-patch](https://github.com/guaguastandup/aider-swebench-patch) and [aider-swe-bench](https://github.com/Aider-AI/aider-swe-bench), create a conda environment named `aider-swe2`, and install dependencies.

```bash
git clone https://github.com/guaguastandup/aider-swebench-patch
cd aider-swebench-patch

git clone https://github.com/Aider-AI/aider-swe-bench
conda create -n aider-swe2 python=3.11
conda activate aider-swe2

cd aider-swe-bench
pip install -r requirements.txt
cd ..
```

**Install Docker Scripts**:
Clone the `SWE-bench-docker` repository, which is required for managing evaluation containers.

```bash
git clone https://github.com/aorwall/SWE-bench-docker
```

**🔩 Apply Patch**:
You **MUST** overwrite the Aider library with the patched version located in `backups/aider`. This fixes specific bugs encountered during SWE-bench execution.

```bash
# 使用 Python 自动查找 site-packages 目录 (适用于任何 conda 环境)
SITE_PKGS=$(python -c "import site; print(site.getsitepackages()[0])")
rm -rf "$SITE_PKGS/aider"
cp -r backups/aider "$SITE_PKGS/"
```

**🔩 Apply Custom Harness**:
Replace the default harness with our custom version that includes retry logic.

```bash
# harness-retry.py and run_swe_task.py are included in this repository
cp harness-retry.py aider-swe-bench/
```

### 2. Running Tasks

Edit the `run_tem.sh` script to execute benchmark tasks.

**Command**:
```bash
./run_tem.sh
```

**Trace Forwarding**:
* **DeepSeek**: Traces are automatically forwarded to `localhost`.
* **OpenAI**: Traces are NOT forwarded and must be fetched manually via the dashboard plugin.

### 3. Trace Acquisition (OpenAI)

If using OpenAI models, follow these steps to extract the trace:

1. **Install Plugin**: Install the Chrome browser extension from the `xpi/` directory.
2. **Access Logs**: Go to [OpenAI API Logs](https://platform.openai.com/logs?api=chat-completions). 
3. **Export**: Click the plugin button on a log entry and select **"Export Trace"** to download the trace data.
4. **Convert**: Save the trace to the appropriate directory and run the conversion script (see `run_swe_task.py` for automated conversion).