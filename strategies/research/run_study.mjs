// Run an OpenScript study over the research 5m bars with the chart's own engine.
//   node strategies/research/run_study.mjs <study.oscript> <bars.json> <out.json>
// The bars file comes from compare_study.py. The output holds every bar's columns.
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const root = resolve(dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), '..', '..');
const lib = await import(pathToFileURL(resolve(root,
  '.claude/skills/openscript/.cache/v0.8.1/node_modules/openalgo-script/dist/core/index.js')).href);
const { sourceFile, DiagnosticBag, parse, check, emit, renderDiagnostics, load } = lib;

const [scriptPath, barsPath, outPath] = process.argv.slice(2);
const text = readFileSync(scriptPath, 'utf8');
const file = sourceFile('study.oscript', text);
const errors = new DiagnosticBag();
const { program } = emit(file, check(file, parse(file, errors), errors), errors);
if (!program) throw new Error(renderDiagnostics(file, errors.ordered()));

const bars = JSON.parse(readFileSync(barsPath, 'utf8'));
const host = { instrument: { symbol: 'NIFTY', exchange: 'NSE_INDEX', interval: '5', timezone: 'Asia/Kolkata',
  session: { start: '09:15', end: '15:30', days: [1, 2, 3, 4, 5] }, hasVolume: false, hasOpenInterest: false } };
const loaded = load(program, { host });
if (!loaded.ok) throw new Error(JSON.stringify(loaded.diagnostic));
const run = loaded.engine.run(bars);
if (run.diagnostic) throw new Error(JSON.stringify(run.diagnostic));
writeFileSync(outPath, JSON.stringify(run.bars.map((b) => b.columns)));
console.log(`ran ${run.bars.length} bars`);
