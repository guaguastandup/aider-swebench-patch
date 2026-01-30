#!/usr/bin/env python3
"""
SWE-bench 任务执行工作流 (优化版 - 动态端口 + 结构化文件存储)
"""

import argparse
import os
import subprocess
import sys
import time
import signal
import logging
import socket
import re  # === 新增 regex 支持 ===
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================

TASK_PORTS = {
    "astropy__astropy-12907": 8891,
    "django__django-11265": 8892,
    "sympy__sympy-13615": 8893,
    "sympy__sympy-13974": 8894,
    "astropy__astropy-13977": 8895,
}

# 路径配置 (支持环境变量、命令行参数、或自动检测)
# 方式1: 环境变量 LM_CACHE_AGENT_ROOT
# 方式2: 自动检测 (脚本所在目录或其父目录)
# 方式3: 手动设置 PROJECT_ROOT (如果需要覆盖)
def get_project_root() -> Path:
    """获取项目根目录，支持多种配置方式"""
    # 1. 优先使用环境变量
    env_root = os.environ.get("LM_CACHE_AGENT_ROOT")
    if env_root:
        return Path(env_root)

    # 2. 自动检测: 检测脚本位置
    script_path = Path(__file__).resolve()

    # 如果脚本在 aider-swebench-patch 目录中，返回父目录
    if script_path.parent.name == "aider-swebench-patch":
        return script_path.parent.parent

    # 如果脚本名为 run_swe_task.py 且直接在项目根目录
    if script_path.name == "run_swe_task.py":
        return script_path.parent

    # 3. 兜底: 使用当前工作目录
    return Path.cwd()

PROJECT_ROOT = get_project_root()
AIDER_DIR = PROJECT_ROOT / "aider"
AIDER_TRACE_DIR = AIDER_DIR / "aider-trace"
AIDER_SWE_BENCH = AIDER_DIR / "aider-swe-bench"

RAW_TRACES_DIR = AIDER_TRACE_DIR / "raw_traces"
TRACES_DIR = AIDER_TRACE_DIR / "traces"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("SWE-Workflow")

# ==================== 路径/命名 辅助工具 (新增) ====================

def parse_path_info(model_name: str, task_name: str) -> Tuple[str, str]:
    """
    解析模型名称，返回 (provider, clean_filename_base)
    例如: 
    input: model="openai/gpt-4o-2024-05-13", task="astropy-1"
    output: ("openai", "gpt-4o__astropy-1")
    """
    # 1. 提取 Provider 和 Model
    if "/" in model_name:
        provider, raw_model = model_name.split("/", 1)
    else:
        provider, raw_model = "unknown", model_name

    # 2. 清洗模型名称 (去除日期格式，如 -2024-05-13, -20240620)
    # 匹配 -YYYY-MM-DD 或 -YYYYMMDD 或 _YYYY...
    clean_model = re.sub(r'[-_]?\d{4}[-_]?\d{2}[-_]?\d{2}|[-_]?\d{8}', '', raw_model)
    
    # 3. 构造基础文件名
    filename_base = f"{clean_model}__{task_name}"
    
    return provider, filename_base

# ==================== 网络/进程 工具类 ====================

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port = s.getsockname()[1]
        return port

class TraceProxy:
    def __init__(self, port: int, output_file: Path):
        self.port = port
        self.output_file = output_file
        self.process = None

    def __enter__(self):
        self._kill_port_if_busy()
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _kill_port_if_busy(self):
        try:
            pid = subprocess.check_output(["lsof", "-t", "-i", f":{self.port}"], stderr=subprocess.DEVNULL).strip()
            if pid:
                logger.warning(f"Port {self.port} is busy, killing PID {int(pid)}...")
                os.kill(int(pid), signal.SIGKILL)
                time.sleep(1)
        except: pass

    def start(self):
        logger.info(f"Starting DeepSeek Tracer on port {self.port}")
        # 确保父目录存在 (provider 文件夹)
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        if self.output_file.exists(): self.output_file.unlink()

        cmd = [sys.executable, "trace.py", "--output", str(self.output_file), "--port", str(self.port)]
        
        self.process = subprocess.Popen(cmd, cwd=AIDER_TRACE_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=os.setsid)
        time.sleep(2)
        if self.process.poll() is not None:
            raise RuntimeError(f"Proxy failed start: {self.process.stderr.read()}")

    def stop(self):
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=5)
            except: pass

def run_command(cmd: List[str], cwd: Path, env: Dict[str, str] = None, desc: str = "", stream_output: bool = False) -> bool:
    if desc: logger.info(f"▶️  {desc}")
    process_env = os.environ.copy()
    if env: process_env.update(env)

    try:
        if stream_output:
            result = subprocess.run(cmd, cwd=cwd, env=process_env, text=True)
        else:
            result = subprocess.run(cmd, cwd=cwd, env=process_env, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"❌ Command failed: {' '.join(cmd)}")
            if not stream_output and result.stderr:
                print("="*20 + " ERROR " + "="*20 + "\n" + result.stderr + "\n" + "="*47)
            return False
        return True
    except Exception as e:
        logger.error(f"❌ Execution error: {e}")
        return False

# ==================== 清理逻辑 ====================

def clean_previous_run(task_name: str, model_name: str):
    """清理 Trace 文件和 Harness 生成的 Prediction 文件"""
    
    # 1. 计算新版路径
    provider, filename_base = parse_path_info(model_name, task_name)
    
    # 可能存在的文件列表 (新命名规范)
    files_to_remove = [
        RAW_TRACES_DIR / provider / f"{filename_base}.jsonl",
        TRACES_DIR / provider / f"{filename_base}.jsonl",
        TRACES_DIR / provider / f"{filename_base}.png"
    ]
    
    # 兼容清理旧版命名 (可选，为了防止混淆)
    safe_model_old = model_name.replace("/", "_").replace("-", "_")
    files_to_remove.extend([
        RAW_TRACES_DIR / f"{safe_model_old}__{task_name}.jsonl",
        TRACES_DIR / f"{safe_model_old}__{task_name}.jsonl",
        TRACES_DIR / f"{safe_model_old}__{task_name}.png"
    ])

    for f in files_to_remove:
        if f.exists():
            logger.info(f"🗑️  Deleting trace: {f.relative_to(AIDER_TRACE_DIR) if f.is_relative_to(AIDER_TRACE_DIR) else f.name}")
            f.unlink()

    # 清理 predictions (这个逻辑保持不变，因为是 harness 决定的)
    preds_dir = AIDER_SWE_BENCH / "predictions"
    if not preds_dir.exists(): return

    safe_model_harness = model_name.replace("/", "-")
    cleaned_pred = False
    for subdir in preds_dir.iterdir():
        if subdir.is_dir() and safe_model_harness in subdir.name:
            pred_file = subdir / f"{task_name}.json"
            if pred_file.exists():
                logger.info(f"🗑️  Deleting prediction: {pred_file.relative_to(preds_dir)}")
                pred_file.unlink()
                cleaned_pred = True
    
    if not cleaned_pred:
        logger.info(f"ℹ️  No previous prediction found for {task_name}")


# ==================== 核心逻辑 ====================

def get_trace_path(task_name: str, model_name: str) -> Path:
    """生成 Raw Trace 路径: raw_traces/{provider}/{clean_model}__{task}.jsonl"""
    provider, filename_base = parse_path_info(model_name, task_name)
    target_dir = RAW_TRACES_DIR / provider
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"{filename_base}.jsonl"

def run_swe_bench_task(task_name: str, model_name: str, port: int, use_proxy: bool, stream_output: bool) -> bool:
    script_name = "harness-retry.py"
    script_path = AIDER_SWE_BENCH / script_name
    if not script_path.exists(): return False

    env = {"LANGUAGE": "English"}
    if use_proxy:
        env["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")
        env["DEEPSEEK_API_BASE"] = f"http://localhost:{port}"
        env["DEEPSEEK_BASE_URL"] = f"http://localhost:{port}"
    else:
        env["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")

    cmd = [sys.executable, script_name, task_name, "--model", model_name]
    return run_command(cmd, cwd=AIDER_SWE_BENCH, env=env, desc=f"Running harness for {task_name}", stream_output=stream_output)

def convert_trace(raw_trace_file: Path, model_name: str, task_name: str) -> Optional[Path]:
    if not raw_trace_file.exists(): return None
    
    # 按照 Provider 分类存储 Converted Trace
    provider, filename_base = parse_path_info(model_name, task_name)
    output_dir = TRACES_DIR / provider
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_trace_file = output_dir / f"{filename_base}.jsonl"
    
    convert_script = "convert.py"
    if not (AIDER_TRACE_DIR / convert_script).exists():
         logger.error(f"❌ convert.py not found in {AIDER_TRACE_DIR}")
         return None

    cmd = [sys.executable, convert_script, str(raw_trace_file), str(output_trace_file)]
    
    if run_command(cmd, cwd=AIDER_TRACE_DIR, desc=f"Converting trace"): 
        return output_trace_file
    return None

def analyze_prefix_hit_rate(trace_file: Path, task_name: str, model_name: str) -> bool:
    # 按照 Provider 分类存储 PNG
    provider, filename_base = parse_path_info(model_name, task_name)
    output_dir = TRACES_DIR / provider
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_png = output_dir / f"{filename_base}.png"
    
    cmd = [sys.executable, "prefix_analysis.py", "-i", str(trace_file), "-o", str(output_png), "--tokenizer", "unsloth/Meta-Llama-3.1-8B"]
    return run_command(cmd, cwd=AIDER_TRACE_DIR, desc=f"Analyzing prefix hit rate")

def run_single_workflow(task_name: str, model_name: str, force_rerun: bool = False, verbose: bool = True) -> bool:
    # 1. 端口分配
    port = TASK_PORTS.get(task_name)
    if not port:
        logger.info(f"Task '{task_name}' has no preset port. Allocating dynamic port...")
        port = get_free_port()
        logger.info(f"✨ Assigned dynamic port: {port}")
    else:
        logger.info(f"✨ Using preset port: {port}")

    is_deepseek = "deepseek" in model_name.lower()
    
    # 获取精简后的名称用于日志显示
    _, filename_base = parse_path_info(model_name, task_name)
    prefix_str = f"[{filename_base}]"
    
    logger.info(f"{prefix_str} 🚀 Starting workflow")

    if force_rerun:
        clean_previous_run(task_name, model_name)

    # === DeepSeek 流程 ===
    if is_deepseek:
        raw_trace_file = get_trace_path(task_name, model_name)
        try:
            with TraceProxy(port, raw_trace_file) as proxy:
                if not run_swe_bench_task(task_name, model_name, port, use_proxy=True, stream_output=verbose):
                    return False
                time.sleep(2)
            
            # 注意：这里需要传入 model_name 和 task_name 以确定输出路径
            converted = convert_trace(raw_trace_file, model_name, task_name)
            if converted: 
                analyze_prefix_hit_rate(converted, task_name, model_name)
            
        except Exception as e:
            logger.exception("Error in workflow") 
            return False
            
    # === OpenAI / 其他模型流程 ===
    else:
        logger.info(f"{prefix_str} Direct connection (No proxy, No trace conversion)")
        
        # 即使是 Placeholder，也放入规范的文件夹
        raw_trace_file = get_trace_path(task_name, model_name)
        try:
            # get_trace_path 已经创建了父目录
            raw_trace_file.touch()
            logger.info(f"📝 Created placeholder trace: {raw_trace_file.relative_to(AIDER_TRACE_DIR)}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to create placeholder trace: {e}")

        if not run_swe_bench_task(task_name, model_name, port, use_proxy=False, stream_output=verbose): 
            return False

    logger.info(f"{prefix_str} ✅ Workflow COMPLETED")
    return True

def run_all_tasks(model_name: str, parallel_workers: int, force_rerun: bool) -> bool:
    tasks = list(TASK_PORTS.keys())
    logger.info(f"🚀 Running {len(tasks)} tasks")
    results = {}
    with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
        future_to_task = {executor.submit(run_single_workflow, task, model_name, force_rerun, verbose=False): task for task in tasks}
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            results[task] = future.result()
            print(f"Task finished: {task} {'✅' if results[task] else '❌'}")
    return all(results.values())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--model", default="deepseek/deepseek-reasoner")
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--list-tasks", action="store_true")
    args = parser.parse_args()

    if args.list_tasks:
        print("Predefined tasks:")
        for t, p in TASK_PORTS.items(): print(f"  {t}: {p}")
        return 0

    if args.task.lower() == "all":
        run_all_tasks(args.model, args.parallel, args.force_rerun)
    else:
        run_single_workflow(args.task, args.model, args.force_rerun, verbose=True)

if __name__ == "__main__":
    sys.exit(main())