#!/usr/bin/env python

import json
import random
import subprocess
import sys
import tempfile
import argparse
import os
import traceback
from pathlib import Path

# 假设这些是你项目中的依赖，保持不变
import lox
from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model, register_litellm_models
from aider.repo import GitRepo

from dump import dump
from tests import run_tests
from utils import get_full_dataset  # noqa: F401
from utils import get_lite_dataset  # noqa: F401
from utils import get_verified_dataset
from utils import get_devin_instance_ids, get_plausible, load_predictions, pick_winner

REPOS_DNAME = Path("repos")
CHAT_LOGS_DNAME = Path("chat-logs")
PREDS_DNAME = Path("predictions")


def diff_versus_commit(git_dname, commit):
    """
    Take a diff of `git_dname` current contents versus the `commit`.
    """
    diff_cmd = f"git -C {git_dname} diff {commit}"
    diff_output = subprocess.check_output(diff_cmd.split()).decode()
    return diff_output


def files_in_patch(patch):
    """
    Extract the list of modified files from a unified diff patch string.
    """
    files = []
    for line in patch.split("\n"):
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            fname = line.split("/", 1)[1]
            if fname not in files:
                files.append(fname)
    return files


def checkout_repo(git_tempdir, entry):
    """
    Clone the SWE Bench entry's git `repo` into `dname` at the `base_commit`.
    Make a tempdir if no `dname` provided.
    """
    github_url = "https://github.com/"
    repo_url = github_url + entry["repo"]
    commit = entry["base_commit"]

    print(repo_url, commit)

    checkout_repo_url_commit(git_tempdir, repo_url, commit)


def checkout_repo_url_commit(repo_dname, url, commit):
    """
    Clone the git `url` into `dname` at `commit`.
    Check a local cache of the bare repo to avoid pulling from github every time.
    """
    # Extract repo name from URL
    repo_name = url.split("/")[-1].split(".")[0]
    repo_name += ".git"

    # dump(repo_name)
    REPOS_DNAME.mkdir(exist_ok=True)
    bare_repo = REPOS_DNAME / repo_name

    if not bare_repo.exists():
        cmd = f"git clone --bare {url} {bare_repo}"
        subprocess.run(cmd.split(), check=True)

    cmd = f"git clone {bare_repo} {repo_dname}"
    subprocess.run(cmd.split(), check=True)

    cmd = f"git -c advice.detachedHead=false -C {repo_dname} checkout {commit}"
    subprocess.run(cmd.split(), check=True)


def show_problems(dataset):
    """
    Print out all the instance_id and problem_descriptions.
    """
    for inst, entry in dataset.items():
        problem = entry["problem_statement"].splitlines()[0]
        print(f"{inst}: {problem}")


def run_pre_existing_tests(entry, git_dname):
    """
    Given the current contents of the `git_dname`, run the tests.
    Returns None if all the tests passed. Returns the text of the
    test run output if any failed.
    """
    model_patch = diff_versus_commit(git_dname, entry["base_commit"])
    passed, output = run_tests(
        entry,
        model_patch=model_patch,
        use_test_patch=False,
    )
    # We were UNABLE to run tests
    if passed is None:
        return

    if passed:
        return

    # Just keep the output after the (no-op) test patch applied,
    # which is the actual output from the tests that were run.
    output = output.split(">>>>> Applied Patch (test)")[-1]

    return output


def get_coder(model, git_dname, chat_history_file, test_cmd, temperature, oracle_files=None):
    """
    Get an instance of aider to work with the given LLM `model` at `temperature`
    on the code in `git_dname`.
    """
    if oracle_files and git_dname:
        oracle_files = [Path(git_dname) / fname for fname in oracle_files]

    model = Model(model)

    io = InputOutput(
        yes=True,  # Say yes to every suggestion aider makes
        chat_history_file=chat_history_file,  # Log the chat here
        input_history_file="/dev/null",  # Don't log the "user input"
    )

    dump(git_dname)
    repo = GitRepo(io, fnames=None, git_dname=git_dname, models=model.commit_message_models())

    coder = Coder.create(
        main_model=model,
        io=io,
        edit_format=None, # None 让 Aider 自动选择, 可以尝试强制 "diff"
        repo=repo,
        map_tokens=2048,
        cache_prompts=True,
        stream=False,
        auto_commits=False,
        fnames=oracle_files,
        auto_test=True,
        test_cmd=test_cmd,
        chat_language="English",
    )
    coder.temperature = temperature

    # Take at most 5 steps before giving up.
    # 稍微调高一点，给 Agent 更多自我修正的机会
    coder.max_reflections = 5

    coder.show_announcements()
    print("coder.test_cmd:", coder.test_cmd)
    return coder


def process_one_instance(entry, num_tries, models, temperature, model_name_or_path, out_dname):
    """Process one `entry` from SWE Bench."""

    instance_id = entry["instance_id"]
    base_commit = entry["base_commit"]

    print("=" * 60)
    dump(instance_id)
    print("=" * 60)
    problem_statement = entry["problem_statement"] 
    print(problem_statement)

    ###
    # DO NOT assist aider by telling it which files need to be modified!
    oracle = False
    gold_files = files_in_patch(entry["patch"])
    if oracle:
        oracle_files = gold_files
    else:
        oracle_files = None
    ###

    chat_history_file = out_dname / (instance_id + ".md")

    if chat_history_file.exists():
        chat_history_file.unlink()

    results = []
    cost = 0
    winner = None

    for attempt in range(1, num_tries + 1):
        for model in models:
            dump(attempt, model)

            with tempfile.TemporaryDirectory(dir="./aider-cache") as git_tempdir:
                dump(git_tempdir)
                checkout_repo(git_tempdir, entry)

                # === 生成调用自身的测试命令 ===
                entry_json_path = Path(git_tempdir) / "entry.json"
                entry_json_path.write_text(json.dumps(entry))

                current_harness_path = Path(__file__).resolve()
                test_cmd = f"{sys.executable} {current_harness_path} --run-test entry.json"
                # ===========================
                
                coder = get_coder(
                    model,
                    git_tempdir,
                    chat_history_file,
                    test_cmd,
                    temperature,
                    oracle_files,
                )

                
                dump(instance_id)
                dump(gold_files)

                # === 【关键修改：优化 Prompt】 ===
                # 显式禁止在未读取文件的情况下进行修改，防止 AI 幻觉
                message = """Below is a real GitHub issue from a popular GitHub repository.
The issue was filed some time ago.
The repo has been checked out at the commit that existed at the moment the issue was filed.
If you are already familiar with this repo, be cautious!
You are working with an old version of the repo!
Filenames, directory names, file contents, etc may be different than what you're used to.

Propose changes to update the repo to fix the problem below.

IMPORTANT INSTRUCTIONS:
1. You are currently in a blank state and have not read any files yet. 
2. You MUST read the relevant files (using `ls` to find them and `read_file` to read them) BEFORE attempting to edit them.
3. Do NOT guess the file content based on the issue description/traceback alone. If you try to edit a file without reading it first, the strict matching will fail.
4. Locate the code, read it, and ONLY THEN propose changes.

# Issue Description:
"""
                message += problem_statement
                
                try:
                    coder.run(message)
                    print("guagua")
                except Exception as coder_err:
                    # 打印详细的 Traceback 以便调试
                    print("\n========== ERROR TRACEBACK ==========")
                    traceback.print_exc()
                    print("=====================================\n")
                    dump(coder_err) 
                    continue

                added_files = coder.get_inchat_relative_files()

                if not added_files:
                    message = """You haven't named any files in this repo.
Remember, this repo is checked out at quite an old commit.
So the file layout and contents may be unfamiliar.

Tell me: which 3-5 files from this repo should I look at to solve the problem?
"""
                    coder.run(message)
                # --- 手动修改 2：带止损机制的迭代 ---
                max_iterations = 3
                last_error = ""
                
                for i in range(max_iterations):
                    print(f"\n>>> Step {i+1}/{max_iterations}")
                    
                    if i == 0:
                        current_prompt = message
                    else:
                        # 运行测试看报错
                        current_error = run_pre_existing_tests(entry, git_tempdir)
                        if not current_error:
                            print("✨ 测试通过，停止迭代。")
                            break
                        
                        # 如果报错内容和上一轮一模一样，说明 AI 卡住了，直接止损
                        if current_error == last_error:
                            print("!!! 错误信息未变化，AI 陷入死循环。正在强制停止以节省 Token。")
                            break
                        
                        last_error = current_error
                        current_prompt = f"The previous fix failed with these errors:\n{current_error}\nPlease try a DIFFERENT approach."

                    coder.run(current_prompt)
                    
                    # 检查是否产生了补丁，产生了就收手
                    model_patch = diff_versus_commit(git_tempdir, base_commit)
                    if model_patch.strip():
                        print("✨ 补丁已生成。")
                        break
                # -------------------------------
              # --- 优化后的最小改动逻辑 ---
                # max_iterations = 3 
                # for i in range(max_iterations):
                #     print(f"\n--- 迭代第 {i+1} 轮 ---")
                    
                #     if i == 0:
                #         # 第一轮：提出问题
                #         current_msg = message
                #     else:
                #         # 第二轮及以后：如果还没改代码，就踢它一脚，让它别光说话
                #         current_msg = "I have provided the files. Now, please focus on generating the SEARCH/REPLACE blocks to fix the issue."

                #     coder.run(current_msg)
                    
                #     # 每一轮跑完，立刻检查是否有代码补丁产出
                #     model_patch = diff_versus_commit(git_tempdir, base_commit)
                #     if model_patch.strip():
                #         print("✨ 关键点：检测到代码改动，立即停止迭代，防止浪费 Token。")
                #         break
                    
                #     # 如果 AI 还是没名字，运行你原本的“没文件名就问文件名”逻辑
                #     added_files = coder.get_inchat_relative_files()
                #     if not added_files and i == 0:
                #         coder.run("You haven't named any files. Tell me: which 3-5 files should I look at?")
                        
              # --- 修改开始：增加多轮循环 ---
              # useless
#                 max_iterations = 3
#                 for i in range(max_iterations):
#                     print(f"--- 自动迭代第 {i+1} 轮 ---")
                    
#                     # 第一轮发送初始问题，后续轮次发送“继续执行”指令
#                     current_msg = message if i == 0 else "Based on the files, please apply the necessary code changes using SEARCH/REPLACE blocks."
#                     coder.run(current_msg)

#                     # 检查是否产生了补丁（核心：有了补丁就提前退出，节省资源）
#                     model_patch = diff_versus_commit(git_tempdir, base_commit)
#                     if model_patch.strip():
#                         print("✨ 检测到代码改动，停止迭代。")
#                         break
                    
#                     # --- 下面是你原本就有的“没名字就问名字”的逻辑 ---
#                     added_files = coder.get_inchat_relative_files()
#                     if not added_files and i == 0: # 仅在第一轮没名字时询问
#                         message_ask = """You haven't named any files in this repo.
# Tell me: which 3-5 files from this repo should I look at to solve the problem?
# """
#                         coder.run(message_ask)
                # --- 修改结束 ---
                
                dump(instance_id)
                dump(gold_files)
                dump(added_files)

                cost += coder.total_cost
                model_patch = diff_versus_commit(git_tempdir, base_commit)
                dump(model_patch)

            result = dict(
                instance_id=instance_id,
                model_name_or_path=model_name_or_path,
                model_patch=model_patch,
                model=model,
                temperature=temperature,
                cost=coder.total_cost,
                added_files=added_files,
                gold_files=gold_files,
                edited_files=files_in_patch(model_patch),
                edit_outcome="",
                lint_outcome=coder.lint_outcome,
                test_outcome=coder.test_outcome,
            )
            result["try"] = attempt
            results.append(result)

            dump(result)

            if model_patch and coder.lint_outcome and coder.test_outcome:
                winner = result
                break

        if winner:
            break

    if not winner:
        winner = pick_winner(results)

    if not winner:
        result = dict(
            instance_id=instance_id,
            model_name_or_path=model_name_or_path,
            model_patch=None,
        )

    dump(winner)
    if not winner:
        return

    print("\n\nFinal diff:\n")
    if winner.get("model_patch"):
        print(winner["model_patch"])
    else:
        print("No patch generated.")

    winner = dict(winner)
    winner.update(
        dict(
            tries=attempt,
            all_results=results,
            cost=cost,
        )
    )

    out_fname = out_dname / (instance_id + ".json")
    out_fname.write_text(json.dumps(winner, indent=4))


def process_instances(
    prefix, dataset, models, num_tries, temperature, threads, prior_dnames, just_devin_570
):
    models_slug = "--".join(model.replace("/", "-") for model in models)
    model_name_or_path = "aider--" + models_slug
    models_slug = prefix + "--" + models_slug

    dump(models)
    dump(temperature)

    out_dname = PREDS_DNAME / models_slug
    if not out_dname.exists():
        out_dname.mkdir(parents=True, exist_ok=True)

    dump(out_dname)

    done_preds = load_predictions([out_dname], just_devin_570)
    done_instances = set(done_preds.keys())

    prior_preds = load_predictions(prior_dnames, just_devin_570)
    plausible_instances = get_plausible(prior_preds)

    if prior_preds:
        all_instances = set(prior_preds.keys())
    else:
        all_instances = set(dataset.keys())

    remaining_instances = set(all_instances)
    remaining_instances -= done_instances
    remaining_instances -= plausible_instances

    remaining_instances = list(remaining_instances)
    random.shuffle(remaining_instances)

    dump(sorted(remaining_instances))
    dump(len(remaining_instances))

    print()
    print("press enter...")
    # input() 

    if not CHAT_LOGS_DNAME.exists():
        CHAT_LOGS_DNAME.mkdir()

    chat_history_dname = CHAT_LOGS_DNAME / models_slug
    chat_history_dname.mkdir(exist_ok=True)

    if threads > 1:
        process_one_instance_lox = lox.process(threads)(process_one_instance)
        process_one_instance_func = process_one_instance_lox.scatter
        gather = process_one_instance_lox.gather
    else:
        process_one_instance_func = process_one_instance

    for instance_id in remaining_instances:
        if instance_id in done_instances:
            print("skipping", instance_id)
            continue

        process_one_instance_func(
            dataset[instance_id],
            num_tries,
            models,
            temperature,
            model_name_or_path,
            out_dname,
        )

        print("#" * 60)

    if threads > 1:
        gather()


def main(target_id, model_name):
    models_json = Path(".aider.models.json")
    if models_json.exists():
        print(f"Registering {models_json}")
        register_litellm_models([str(models_json)])

    prefix = "terse-udiff"
    print(f"🚀 Using Model: {model_name}")
    models = [model_name]
    num_tries = 1
    temperature = 0

    # dataset = get_full_dataset()
    dataset = get_verified_dataset()

    if target_id in dataset:
        dataset = {target_id: dataset[target_id]}
    else:
        raise ValueError(f"错误：在当前数据集中找不到 ID {target_id}")
    
    print("只跑这一条 ID:", target_id)
    
    just_devin_570 = False
    threads = 1 
    prior_dnames = []

    process_instances(
        prefix, dataset, models, num_tries, temperature, threads, prior_dnames, just_devin_570
    )


if __name__ == "__main__":
    # =================================================================
    # 分支 1: 作为 Test Runner 被 Aider 调用
    # =================================================================
    if len(sys.argv) > 2 and sys.argv[1] == "--run-test":
        try:
            entry_file = sys.argv[2]
            with open(entry_file, "r") as f:
                entry_data = json.load(f)
            
            # 创建缓存目录
            Path("aider-cache").mkdir(exist_ok=True)

            # === 【关键修改：设置 PYTHONPATH】 ===
            # 将当前工作目录添加到 PYTHONPATH，确保测试脚本能导入项目代码
            current_cwd = os.getcwd()
            if "PYTHONPATH" in os.environ:
                # --- 手动修改 3：忽略环境警告 ---
                os.environ["PYTHONWARNINGS"] = "ignore"
                # ----------------------------
                os.environ["PYTHONPATH"] = f"{current_cwd}:{os.environ['PYTHONPATH']}"
            else:
                os.environ["PYTHONWARNINGS"] = "ignore"
                os.environ["PYTHONPATH"] = current_cwd
            # ===================================

            # 运行测试 (git_dname=".")
            error_output = run_pre_existing_tests(entry_data, ".")
            
            if error_output:
                print(error_output)
                sys.exit(1) # 失败
            else:
                print("All tests passed!")
                sys.exit(0) # 成功
        except Exception as e:
            print(f"Test runner error: {e}")
            traceback.print_exc()
            sys.exit(1)

    # =================================================================
    # 分支 2: 用户启动的主程序 (Benchmark Harness)
    # =================================================================
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("target", help="Task ID (e.g., astropy__astropy-13977)")
        parser.add_argument("--model", default="openai/gpt-4o-2024-05-13", help="Model name") 
        
        args = parser.parse_args()
        
        main(args.target, args.model)
        sys.exit(0)