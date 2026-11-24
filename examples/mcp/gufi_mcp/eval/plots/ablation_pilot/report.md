# GUFI MCP server ablation

8 config(s), 15 scenario(s), 1200 graded trial(s).

## Configs

| config | factors | pass | 95% CI | one-shot | tool calls | discovery | aggregate | errors | tokens | cost $ | s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| full | pydantic, schema=hints, primer=1, fn=1, server | 63% | 55%–70% | 55% | 4.2 | 3.0 | 1.16 | 0.14 | 21653 | 0.0024 | 8.8 |
| minimal | plain, schema=none, primer=0, fn=0, rpath | 1% | 0%–4% | 0% | 3.0 | 0.0 | 2.99 | 2.97 | 3214 | 0.0006 | 7.9 |
| naive_schema | pydantic, schema=naive, primer=1, fn=1, server | 55% | 47%–62% | 51% | 4.4 | 3.1 | 1.22 | 0.14 | 16143 | 0.0017 | 7.4 |
| no_functions | pydantic, schema=hints, primer=1, fn=0, server | 55% | 47%–63% | 51% | 3.7 | 2.6 | 1.14 | 0.11 | 16809 | 0.0017 | 7.9 |
| no_primer | pydantic, schema=hints, primer=0, fn=1, server | 44% | 36%–52% | 28% | 2.6 | 0.9 | 1.71 | 0.53 | 8695 | 0.0012 | 8.1 |
| no_schema | pydantic, schema=none, primer=1, fn=1, server | 36% | 29%–44% | 27% | 3.2 | 1.8 | 1.45 | 0.48 | 11849 | 0.0012 | 7.6 |
| plain | plain, schema=hints, primer=1, fn=1, server | 56% | 48%–64% | 44% | 4.5 | 3.1 | 1.33 | 0.25 | 15843 | 0.0017 | 8.5 |
| rpath | pydantic, schema=hints, primer=1, fn=1, rpath | 61% | 53%–69% | 56% | 4.8 | 3.6 | 1.20 | 0.21 | 24998 | 0.0027 | 8.2 |

## Value of each part

Pass rate with the part minus without it, in percentage points, averaged over config pairs that differ only in that part. Intervals: 95% bootstrap over scenarios. *Targeted* = scenarios whose `stresses` name the part.

| part | pairs | effect (pp) | 95% CI | targeted (pp) | other (pp) | Δ tokens/trial | verdict |
|---|---|---|---|---|---|---|---|
| Primer (server instructions) | 1 | +18.7 | +5.3 to +32.0 | +10.0 (n=8) | +28.6 (n=7) | +12958 | keep: helps |
| Functions tools + column tags | 1 | +7.3 | -2.0 to +20.7 | +36.7 (n=3) | +0.0 (n=12) | +4844 | no detectable gain; costs more: simplify candidate |
| Schema tool (names + types vs none) | 1 | +18.7 | -3.3 to +40.7 | +52.5 (n=4) | +6.4 (n=11) | +4295 | no detectable gain; costs more: simplify candidate |
| Schema hints (hints vs names + types) | 1 | +8.0 | -2.7 to +22.7 | +17.5 (n=4) | +4.5 (n=11) | +5510 | no detectable gain; costs more: simplify candidate |
| Schema tool with hints (vs none) | 1 | +26.7 | +4.7 to +50.0 | +70.0 (n=4) | +10.9 (n=11) | +9805 | keep: helps |
| Server path columns (vs agent rpath) | 1 | +1.3 | -11.3 to +14.7 | +12.0 (n=5) | -4.0 (n=10) | -3345 | no detectable effect |
| Structured submission (vs gufi_query command) | 1 | +6.7 | -7.3 to +22.7 | -10.0 (n=1) | +7.9 (n=14) | +5810 | no detectable gain; costs more: simplify candidate |

### Per-scenario effect (pp)

| scenario | stresses | primer | functions | schema_tool | schema_hints | schema_full | path_columns | pydantic |
|---|---|---|---|---|---|---|---|---|
| age_histogram | functions | +30.0 | +30.0 | -80.0 | +30.0 | -50.0 | -60.0 | -40.0 |
| avg_size_per_user | primer, submission | -40.0 | -10.0 | +10.0 | -10.0 | +0.0 | +0.0 | -10.0 |
| count_and_size | primer | +30.0 | +10.0 | +80.0 | +10.0 | +90.0 | +0.0 | +20.0 |
| dirs_many_files | schema, path | +0.0 | +0.0 | +90.0 | +0.0 | +90.0 | +0.0 | +0.0 |
| executable_count | schema | +40.0 | -10.0 | +0.0 | +90.0 | +90.0 | +20.0 | +70.0 |
| ext_winner | functions, primer | +40.0 | +80.0 | +20.0 | +20.0 | +40.0 | +10.0 | +60.0 |
| files_and_bytes_per_user | primer | +0.0 | -10.0 | +10.0 | -10.0 | +0.0 | +10.0 | -10.0 |
| not_modified_n_days | primer | +40.0 | +10.0 | +10.0 | +0.0 | +10.0 | +0.0 | +40.0 |
| size_histogram | functions | +0.0 | +0.0 | +0.0 | +0.0 | +0.0 | -20.0 | -20.0 |
| subtree_usage | path | +70.0 | +20.0 | +30.0 | +0.0 | +30.0 | +70.0 | +0.0 |
| symlink_targets | schema, path | +10.0 | +0.0 | +100.0 | +0.0 | +100.0 | +0.0 | +0.0 |
| top10_size_global | primer, path | +0.0 | +0.0 | +0.0 | +0.0 | +0.0 | +0.0 | +0.0 |
| top10_size_per_uid | primer, path | +10.0 | +0.0 | +0.0 | +10.0 | +10.0 | -10.0 | -30.0 |
| total_dir_size | schema | +50.0 | +0.0 | +20.0 | -20.0 | +0.0 | +0.0 | +20.0 |
| users_per_group | primer | +0.0 | -10.0 | -10.0 | +0.0 | -10.0 | +0.0 | +0.0 |

## Pass rate by scenario

| scenario | full | minimal | naive_schema | no_functions | no_primer | no_schema | plain | rpath |
|---|---|---|---|---|---|---|---|---|
| age_histogram | 30% | 0% | 0% | 0% | 0% | 80% | 70% | 90% |
| avg_size_per_user | 0% | 0% | 10% | 10% | 40% | 0% | 10% | 0% |
| count_and_size | 100% | 0% | 90% | 90% | 70% | 10% | 80% | 100% |
| dirs_many_files | 100% | 0% | 100% | 100% | 100% | 10% | 100% | 100% |
| executable_count | 90% | 0% | 0% | 100% | 50% | 0% | 20% | 70% |
| ext_winner | 80% | 0% | 60% | 0% | 40% | 40% | 20% | 70% |
| files_and_bytes_per_user | 80% | 0% | 90% | 90% | 80% | 80% | 90% | 70% |
| not_modified_n_days | 100% | 0% | 100% | 90% | 60% | 90% | 60% | 100% |
| size_histogram | 0% | 0% | 0% | 0% | 0% | 0% | 20% | 20% |
| subtree_usage | 70% | 0% | 70% | 50% | 0% | 40% | 70% | 0% |
| symlink_targets | 100% | 0% | 100% | 100% | 90% | 0% | 100% | 100% |
| top10_size_global | 100% | 0% | 100% | 100% | 100% | 100% | 100% | 100% |
| top10_size_per_uid | 10% | 0% | 0% | 10% | 0% | 0% | 40% | 20% |
| total_dir_size | 80% | 10% | 100% | 80% | 30% | 80% | 60% | 80% |
| users_per_group | 0% | 0% | 0% | 10% | 0% | 10% | 0% | 0% |

## Failed tool calls per trial, by error category

| config | plain command form | other | unknown column | SQLite runtime error | path column on wrong table | SQL parse error | no path columns (rpath mode) | column count mismatch | stage read wrong table | path column not carried |
|---|---|---|---|---|---|---|---|---|---|---|
| full | 0.00 | 0.03 | 0.05 | 0.03 | 0.01 | 0.00 | 0.00 | 0.01 | 0.00 | 0.00 |
| minimal | 2.15 | 0.63 | 0.11 | 0.00 | 0.00 | 0.01 | 0.06 | 0.00 | 0.01 | 0.00 |
| naive_schema | 0.00 | 0.04 | 0.03 | 0.01 | 0.01 | 0.04 | 0.00 | 0.01 | 0.00 | 0.00 |
| no_functions | 0.00 | 0.01 | 0.07 | 0.01 | 0.01 | 0.00 | 0.00 | 0.01 | 0.00 | 0.00 |
| no_primer | 0.00 | 0.27 | 0.23 | 0.00 | 0.01 | 0.01 | 0.00 | 0.01 | 0.00 | 0.00 |
| no_schema | 0.00 | 0.06 | 0.23 | 0.10 | 0.06 | 0.01 | 0.00 | 0.01 | 0.00 | 0.00 |
| plain | 0.01 | 0.05 | 0.13 | 0.01 | 0.01 | 0.03 | 0.00 | 0.00 | 0.01 | 0.01 |
| rpath | 0.00 | 0.03 | 0.10 | 0.04 | 0.00 | 0.01 | 0.02 | 0.00 | 0.00 | 0.00 |

## Runs

- full: `ablation_full_20261111T065638Z-7eacd1`
- minimal: `ablation_minimal_20261111T070229Z-7c8fb7`
- naive_schema: `ablation_naive_schema_20261111T072245Z-c412ce`
- no_functions: `ablation_no_functions_20261111T071246Z-0baf39`
- no_primer: `ablation_no_primer_20261111T070733Z-c00570`
- no_schema: `ablation_no_schema_20261111T071753Z-54dae6`
- plain: `ablation_plain_20261111T073256Z-0d3da4`
- rpath: `ablation_rpath_20261111T072736Z-986245`
