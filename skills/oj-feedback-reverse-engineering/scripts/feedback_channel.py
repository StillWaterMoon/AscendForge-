#!/usr/bin/env python3
"""Exact local arithmetic for OJ mismatch-percentage probes (Python 3.8+).

No network, no submission, no third-party dependencies. CLI JSON goes to stdout.
The copysign-case command is an explicitly case-specific replay; all other
commands are generic. See --help and references/probe-recipes.md for examples.
"""

import sys

if sys.version_info < (3, 8):
    sys.exit('ERROR: Python 3.8+ is required; current interpreter: ' +
             sys.version.split()[0] + '. Choose a newer Python executable.')

import argparse
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path


def scale_for(digits):
    if not 0 <= digits <= 9:
        raise ValueError('digits must be between 0 and 9')
    return 100 * 10 ** digits


def ceil_div(a, b):
    return -((-a) // b)


def parse_percent(value, digits):
    """Accept '25.11' or '25.11%', reject values off the requested grid."""
    try:
        number = Decimal(str(value).strip().rstrip('%'))
    except InvalidOperation as error:
        raise ValueError('invalid percentage: ' + str(value)) from error
    if not number.is_finite() or not 0 <= number <= 100:
        raise ValueError('percentage must be finite and in [0, 100]')
    ticks = number * (10 ** digits)
    if ticks != ticks.to_integral_value():
        raise ValueError('percentage has more precision than --digits')
    return int(ticks)


def percent_text(code, digits):
    factor = 10 ** digits
    return (str(code // factor) if digits == 0 else
            '{}.{:0{}d}'.format(code // factor, code % factor, digits)) + '%'


def round_count(length, code, scale):
    if length <= 0 or not 0 <= code <= scale:
        raise ValueError('length must be positive and code in [0, scale]')
    # Same decomposition as a bounded-width host implementation.
    return (length // scale) * code + ((length % scale) * code + scale // 2) // scale


def display_code(length, errors, scale, rounding='half-up'):
    if length <= 0 or not 0 <= errors <= length:
        raise ValueError('require positive length and 0 <= errors <= length')
    if rounding == 'floor':
        return errors * scale // length
    if rounding != 'half-up':
        raise ValueError('unsupported rounding mode')
    return (2 * errors * scale + length) // (2 * length)


def error_interval(length, code, scale, rounding='half-up', slack=0):
    """Integer E range allowing +/- slack display ticks; None if impossible."""
    if length <= 0 or not 0 <= code <= scale or slack < 0:
        raise ValueError('invalid length, code, or slack')
    lower_q, upper_q = max(0, code - slack), min(scale, code + slack)
    if rounding == 'half-up':
        low = ceil_div(length * (2 * lower_q - 1), 2 * scale)
        high = ceil_div(length * (2 * upper_q + 1), 2 * scale) - 1
    elif rounding == 'floor':
        low = ceil_div(length * lower_q, scale)
        high = ceil_div(length * (upper_q + 1), scale) - 1
    else:
        raise ValueError('unsupported rounding mode')
    low, high = max(0, low), min(length, high)
    return (low, high) if low <= high else None


def merge_crt(residues, moduli):
    """Generalized CRT, including consistent non-coprime moduli."""
    if len(residues) != len(moduli) or not residues:
        raise ValueError('residues and moduli must be nonempty lists of equal length')
    x, modulus = 0, 1
    for residue, next_modulus in zip(residues, moduli):
        if next_modulus <= 0:
            raise ValueError('moduli must be positive')
        residue %= next_modulus
        divisor = math.gcd(modulus, next_modulus)
        if (residue - x) % divisor:
            raise ValueError('inconsistent congruences')
        reduced = next_modulus // divisor
        if reduced == 1:
            step = 0
        else:
            inverse = pow(modulus // divisor, -1, reduced)
            step = ((residue - x) // divisor * inverse) % reduced
        x += modulus * step
        modulus *= reduced
        x %= modulus
    return x, modulus


def copysign_round3_counts(n):
    if n < 10000:
        return 0, n
    high = n // 5000000
    if high >= 64:
        return n, 0
    lower = 10000 - 10000 // (high + 1)
    width = 10000 // (high + 1) - 10000 // (high + 2)
    if width >= 5:
        return round_count(n, lower + 2, 10000), round_count(n, width - 5, 10000)
    return round_count(n, lower + width // 2, 10000), 0


def replay_copysign(rows):
    """Decode the documented 3-round design, retaining inconsistent candidates."""
    names = ['float32', 'float16', 'int8', 'int16', 'int32']
    buckets = [{} for _ in names]
    for dtype in range(5):
        for n in range(1, 10000):
            q = display_code(n, round_count(n, 500 + 2000 * dtype + n % 1000, 10000), 10000)
            buckets[dtype].setdefault(q, []).append(n)
    output = []
    for row in rows:
        q1, q2, q3 = [parse_percent(row[key], 2) for key in ('round1', 'round2', 'round3')]
        candidates = []
        if q2 < 5000:
            for dtype in range(5):
                for rank, n in enumerate(buckets[dtype].get(q1, [])):
                    errors = (n * (rank + 1) + 50) // 100
                    if display_code(n, errors, 10000) == q2:
                        candidates.append((dtype, n))
        else:
            if q3 == 10000:
                output.append({'point': row['point'], 'status': 'range-overflow',
                               'message': 'N >= 320000000; upper digits unresolved'})
                continue
            highs = [h for h in range(64)
                     if 10000 - 10000 // (h + 1) <= q3 < 10000 - 10000 // (h + 2)]
            # Large-case round 1 has one unused 1000-code gap per dtype.
            for dtype in range(5):
                low = q1 - 500 - 2000 * dtype
                if not 0 <= low < 1000:
                    continue
                residue = (q2 - 5000) * 1000 + low
                for high in highs:
                    n = high * 5000000 + residue
                    if n < 10000:
                        continue
                    if display_code(n, round_count(n, 500 + 2000 * dtype + n % 1000, 10000), 10000) != q1:
                        continue
                    if display_code(n, round_count(n, 5000 + (n // 1000) % 5000, 10000), 10000) != q2:
                        continue
                    candidates.append((dtype, n))
        decoded = []
        for dtype, n in candidates:
            base, sample = copysign_round3_counts(n)
            interval = error_interval(n, q3, 10000)
            if interval is None:
                continue
            positive_min = max(0, interval[0] - base)
            positive_max = min(sample, interval[1] - base)
            if positive_min > positive_max:
                continue
            result = {'dtype': names[dtype], 'N': n, 'sample_start': base,
                      'sample_length': sample, 'positive_count_interval': [positive_min, positive_max],
                      'statistic_scope': 'full-input' if n < 10000 else 'contiguous-window'}
            if sample:
                result['positive_percent_interval'] = [100 * positive_min / sample,
                                                       100 * positive_max / sample]
            decoded.append(result)
        output.append({'point': row['point'], 'status': 'unique' if len(decoded) == 1 else
                       ('inconsistent' if not decoded else 'ambiguous'), 'candidates': decoded})
    return {'model': 'exact-rational-half-up; full-element-comparison',
            'warning': 'Case-specific parameters. Float32 display error not modeled; retain ambiguity if observed.',
            'results': output}


def integer_list(text):
    return [int(part.strip()) for part in text.split(',') if part.strip()]


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)

    def common(command, length=True, rounding=True):
        if length:
            command.add_argument('--length', type=int, required=True, help='compared output element count M')
        command.add_argument('--digits', type=int, default=2, help='decimal digits in displayed percentage')
        if rounding:
            command.add_argument('--rounding', choices=['half-up', 'floor'], default='half-up')

    display = commands.add_parser('display', help='simulate exact percentage display from M and E')
    common(display)
    display.add_argument('--errors', type=int, required=True)
    interval = commands.add_parser('interval', help='recover possible E from M and displayed percentage')
    common(interval)
    interval.add_argument('--percent', required=True)
    interval.add_argument('--code-slack', type=int, default=0, help='extra possible error in displayed ticks')
    plan = commands.add_parser('plan', help='map a code to an error count and simulate its feedback')
    common(plan)
    plan.add_argument('--code', type=int, required=True)
    scan = commands.add_parser('enumerate', help='enumerate size/dtype under a parameterized residue probe')
    common(scan, length=False)
    scan.add_argument('--min-size', type=int, default=1)
    scan.add_argument('--max-size', type=int, required=True, help='inclusive upper bound')
    scan.add_argument('--dtype-codes', default='0,1,2,3,4')
    scan.add_argument('--base', type=int, required=True)
    scan.add_argument('--dtype-step', type=int, required=True)
    scan.add_argument('--modulus', type=int, required=True)
    scan.add_argument('--percent', required=True)
    scan.add_argument('--code-slack', type=int, default=0)
    crt = commands.add_parser('crt', help='merge congruences, report a residue and period, never an unbounded exact value')
    crt.add_argument('--residues', required=True)
    crt.add_argument('--moduli', required=True)
    case = commands.add_parser('copysign-case', help='replay the documented CopySign observations')
    case.add_argument('--observations', type=Path, default=Path(__file__).resolve().parents[1] /
                      'references' / 'copysign-observations.json')
    return parser


def main():
    args = build_parser().parse_args()
    if args.command == 'crt':
        value, period = merge_crt(integer_list(args.residues), integer_list(args.moduli))
        result = {'residue': value, 'modulus': period,
                  'meaning': 'z = residue + k * modulus; bounds needed for uniqueness'}
    elif args.command == 'copysign-case':
        result = replay_copysign(json.loads(args.observations.read_text(encoding='utf-8')))
    else:
        scale = scale_for(args.digits)
        if args.command == 'display':
            code = display_code(args.length, args.errors, scale, args.rounding)
            result = {'code': code, 'percent': percent_text(code, args.digits), 'model': args.rounding}
        elif args.command == 'interval':
            code = parse_percent(args.percent, args.digits)
            interval = error_interval(args.length, code, scale, args.rounding, args.code_slack)
            result = {'errors': list(interval) if interval else None,
                      'unique': bool(interval and interval[0] == interval[1]),
                      'model': args.rounding, 'code_slack': args.code_slack}
        elif args.command == 'plan':
            errors = round_count(args.length, args.code, scale)
            observed = display_code(args.length, errors, scale, args.rounding)
            result = {'errors': errors, 'predicted_code': observed,
                      'predicted_percent': percent_text(observed, args.digits),
                      'recovers_requested_code': observed == args.code, 'model': args.rounding}
        else:
            if not 1 <= args.min_size <= args.max_size or args.modulus <= 0 or args.code_slack < 0:
                raise ValueError('invalid size bounds, modulus, or slack')
            target = parse_percent(args.percent, args.digits)
            candidates = []
            for dtype in integer_list(args.dtype_codes):
                for n in range(args.min_size, args.max_size + 1):
                    code = args.base + args.dtype_step * dtype + n % args.modulus
                    errors = round_count(n, code, scale)
                    observed = display_code(n, errors, scale, args.rounding)
                    if abs(observed - target) <= args.code_slack:
                        candidates.append({'dtype_code': dtype, 'size': n, 'predicted_code': observed})
            result = {'formula': 'c = base + dtype_step*dtype_code + size%modulus; M=size',
                      'candidates': candidates, 'count': len(candidates),
                      'scope': [args.min_size, args.max_size], 'model': args.rounding}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as error:
        sys.exit('ERROR: ' + str(error))
