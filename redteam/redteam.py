import random
import os
from dotenv import load_dotenv
import argparse
import itertools
import json
from typing import Literal

import dspy
import instructor
import openai
import polars as pl
from dspy.teleprompt import MIPRO
from dspy.evaluate import Evaluate
from openai import OpenAI
from tqdm import tqdm

from utils import get_response, judge_prompt
import module_basic
import module_residual
import module_residual_buffer

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
instructor_client = instructor.from_openai(OpenAI())

attack_model = dspy.OpenAI(model="gpt-3.5-turbo-instruct", max_tokens=512)
dspy.settings.configure(lm=attack_model)

target_client = OpenAI(
    api_key=os.getenv("TOGETHER_API_KEY"),
    base_url="https://api.together.xyz/v1",
)
target_model_name = "lmsys/vicuna-7b-v1.5"


def choose_attack_program(choice: Literal["basic", "residual", "buffered"], **settings):
    if choice == "basic":
        return module_basic.AttackProgram(settings["num_layers"])
    elif choice == "residual":
        return module_residual.AttackProgram(settings["num_layers"])
    elif choice == "buffered":
        return module_residual_buffer.AttackProgram(
            settings["num_layers"], settings["buf_size"], settings["critique_model"]
        )


def metric(intent, attack_prompt, trace=None, eval_round=True):
    response = get_response(
        target_client,
        target_model_name,
        attack_prompt,
        inference_params={"max_tokens": 512, "temperature": 0},
    )
    score = judge_prompt(instructor_client, intent, response)[0]
    if eval_round:
        score = round(score)
    return score


def eval_program_manual(prog, eval_set):
    score = 0
    for ex in tqdm(eval_set, desc="Evaluation"):
        result = prog(harmful_intent=ex.harmful_intent)
        score += metric(
            intent=ex.harmful_intent, attack_prompt=result.attack_prompt, eval_round=True
        )
    return score


def load_trainset() -> list[dspy.Example]:
    with open("advbench_subset.json", "r") as f:
        goals = json.load(f)["goals"]

    trainset = [
        dspy.Example(harmful_intent=goal).with_inputs("harmful_intent")
        for goal in goals
    ]
    return trainset


def load_datasets(split: bool = False) -> tuple[list[dspy.Example], list[dspy.Example]]:
    with open("advbench_subset.json", "r") as f:
        goals = json.load(f)["goals"]

    trainset = [
        dspy.Example(harmful_intent=goal).with_inputs("harmful_intent")
        for goal in goals
    ]
    random.shuffle(trainset)
    
    if split:
        trainset, valset = trainset[:int(0.8*len(trainset))], trainset[int(0.8*len(trainset)):]
    else:
        valset = trainset
    return trainset, valset


def evaluate_baseline(valset: list[dspy.Example]) -> int | float:
    # Evaluate baseline: directly passing in harmful intent strings
    print("--- Evaluating Raw Harmful Intent Strings ---")
    base_score = 0
    for ex in tqdm(valset, desc="Raw Input Score"):
        base_score += metric(
            intent=ex.harmful_intent, attack_prompt=ex.harmful_intent, eval_round=True
        )
    base_score /= len(valset)
    print(f"Baseline Score: {base_score}")

    return base_score


def eval_program(prog, valset: list[dspy.Example]) -> float:
    evaluate = Evaluate(
        devset=valset,
        metric=lambda x, y: metric(x, y),
        num_threads=4,
        display_progress=True,
        display_table=0,
    )
    return evaluate(prog)


def compile_program(
        prog, 
        trainset: list[dspy.Example], 
        num_trials: int = 2, 
        num_threads: int = 4,
) -> dspy.Program:
    print("\n--- Compiling Architecture ---")
    optimizer = MIPRO(metric=metric, verbose=True, view_data_batch_size=3)
    best_prog = optimizer.compile(
        prog,
        trainset=trainset,
        max_bootstrapped_demos=2,
        max_labeled_demos=0,
        num_trials=num_trials,
        requires_permission_to_run=False,
        eval_kwargs=dict(num_threads=num_threads, display_progress=True, display_table=0),
    )

    return best_prog


def choose_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument("-s", "--save", action="store_true")
    parser.add_argument("--savefile", type=str, default="results.csv")

    parser.add_argument("--attack_program", choices=["basic", "residual", "buffered"], nargs="+", default="basic")
    parser.add_argument("--num_layers", type=int, nargs="+", default=5)
    parser.add_argument("--buf_size", type=int, nargs="+", default=1)
    parser.add_argument("--critique_model", type=str, nargs="+", default="gpt-3.5-turbo-instruct")
    parser.add_argument("--split_trainset", action="store_true")

    parser.add_argument("--num_threads", type=int, default=4, help="Number of threads to use for optimization")

    args = parser.parse_args()

    args.attack_program = [args.attack_program] if isinstance(args.attack_program, str) else args.attack_program
    args.num_layers = [args.num_layers] if isinstance(args.num_layers, int) else args.num_layers
    args.buf_size = [args.buf_size] if isinstance(args.buf_size, int) else args.buf_size
    args.critique_model = [args.critique_model] if isinstance(args.critique_model, str) else args.critique_model

    return args


def get_setting_combinations(args):
    settings = []
    for attack_program, num_layers in itertools.product(args.attack_program, args.num_layers):
        buf_crit_it = (
            itertools.product(args.buf_size, args.critique_model) 
            if attack_program == "buffered" 
            else [(0, "")]
        )
        settings.extend(
            [
                dict(
                    attack_program=attack_program,
                    num_layers=num_layers,
                    buf_size=buf_size,
                    critique_model=critique_model,
                )
                for buf_size, critique_model in buf_crit_it
            ]
        )
    return settings


def main():
    args = choose_args()
    settings = get_setting_combinations(args)
    trainset, valset = load_datasets(split=args.split_trainset)
    for i, setting in enumerate(settings):
        print(f"\n\n\n\n--- Running Experiment {i+1}/{len(settings)} ---")
        print(f"- Attack Program: {setting['attack_program']}")
        print(f"- Number of Layers: {setting['num_layers']}")
        print(f"- Buffer Size: {setting['buf_size']}")
        print(f"- Critique Model: {setting['critique_model']}\n\n\n\n")
        
        prog = choose_attack_program(
            choice=setting["attack_program"], 
            num_layers=setting["num_layers"],
            buf_size=setting["buf_size"],
            critique_model=setting["critique_model"],
        )
        base_score = evaluate_baseline(valset)
        initial_score = eval_program(prog, valset)
        optimized_scores = []
        for i in range(15):
            prog = compile_program(prog, trainset, num_trials=2, num_threads=args.num_threads)
            optimized_scores.append(eval_program(prog, valset))

        results = {
            "baseline": [base_score],
            "initial": [initial_score],
            "optimized": [str(optimized_scores)],
            "attack_program": [setting["attack_program"]],
            "num_layers": [setting["num_layers"]],
            "buf_size": [setting["buf_size"]],
            "critique_model": [setting["critique_model"]],
            "split_trainset": [args.split_trainset],
        }

        if args.save:
            df = pl.DataFrame(results)
            if not os.path.exists(args.savefile):
                df.write_csv(args.savefile)
            else:
                with open(args.savefile, "ab") as f:
                    df.write_csv(f, include_header=False)


if __name__ == "__main__":
    main()
