"""Paired deals/seat rotations. Reports score differences, never a strength guarantee.

Example: python tests/benchmark_bots.py --candidate advanced --baseline normal --deals 8
"""
import argparse
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game.bot_policy import RandomPolicy
from game.bot_search import AdvancedPolicy
from game.bot_strategy import NormalPolicy
from tests.bot_fixtures import fixed_game, play_match


def policy(kind, seed, budget):
    if kind == 'random':
        return RandomPolicy(random.Random(seed))
    if kind == 'advanced':
        return AdvancedPolicy(budget=budget, seed=seed)
    return NormalPolicy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', choices=['normal', 'advanced'], default='advanced')
    parser.add_argument('--baseline', choices=['random', 'normal'], default='normal')
    parser.add_argument('--opponents', choices=['random', 'normal'], default='normal')
    parser.add_argument('--deals', type=int, default=8)
    parser.add_argument('--seed', type=int, default=100)
    parser.add_argument('--budget', type=float, default=.6)
    parser.add_argument('--output')
    args = parser.parse_args()
    started = time.perf_counter()
    differences, clusters, diagnostics = [], [], []
    role_differences = {'taker': [], 'defense': []}
    contracts = ['prise', 'garde', 'jiedi', 'sans', 'contre']
    for index in range(args.deals):
        seed, contract = args.seed + index, contracts[index % 5]
        cluster = []
        for focus in range(4):
            scores = []
            for kind in (args.baseline, args.candidate):
                policies = [policy(args.opponents, seed * 16 + i, args.budget) for i in range(4)]
                policies[focus] = policy(kind, seed * 16 + focus, args.budget)
                game = fixed_game(seed, contract)
                scores.append(play_match(game, policies, diagnostics if kind == args.candidate else None)['scores'][focus])
            difference = scores[1] - scores[0]
            differences.append(difference)
            cluster.append(difference)
            role_differences['taker' if focus == seed % 4 else 'defense'].append(difference)
        clusters.append(statistics.mean(cluster))
        print(json.dumps(dict(deal=index + 1, contract=contract, paired_mean=round(statistics.mean(differences), 3))), flush=True)
    mean = statistics.mean(differences)
    error = statistics.stdev(clusters) / math.sqrt(len(clusters)) if len(clusters) > 1 else 0
    times = sorted(d['elapsed_ms'] for d in diagnostics if 'elapsed_ms' in d)
    report = dict(candidate=args.candidate, baseline=args.baseline, opponents=args.opponents,
                  deals=args.deals, seed=args.seed, rotated_pairs=len(differences),
                  mean_score_improvement=round(mean, 3),
                  approximate_cluster_95_interval=[round(mean - 1.96 * error, 3), round(mean + 1.96 * error, 3)],
                  by_role={role: round(statistics.mean(values), 3) for role, values in role_differences.items()},
                  budget=args.budget, average_search_samples=round(statistics.mean(d.get('samples', 0) for d in diagnostics), 2) if diagnostics else None,
                  decision_ms_p95=times[min(len(times) - 1, int(len(times) * .95))] if times else None,
                  changed_from_normal=sum(not d.get('fallback', True) for d in diagnostics),
                  elapsed_seconds=round(time.perf_counter() - started, 2))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
