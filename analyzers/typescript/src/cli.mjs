import { analyze } from './index.mjs';

const maxInputBytes = 64 * 1024 * 1024;
let input = '';
let size = 0;
try {
  process.stdin.setEncoding('utf8');
  for await (const part of process.stdin) {
    size += Buffer.byteLength(part, 'utf8');
    if (size > maxInputBytes) throw new Error('Analyzer input exceeds the 64 MiB limit.');
    input += part;
  }
  const records = analyze(JSON.parse(input));
  for (const record of records) process.stdout.write(`${JSON.stringify(record)}\n`);
} catch (error) {
  process.stderr.write(`Analysis failed: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
