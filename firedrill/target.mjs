import { fileURLToPath } from 'node:url';
import { resolve, join } from 'node:path';
import { ApplicationError, commandMain, invokeCLI, privateDirectory } from './adapter-runtime.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const cases = new Set(['ordinary', 'unclear-owner', 'prior-complete', 'uncertain-notice']);

export async function runApplication(input, parameters, signal, invocation) {
  if (Object.keys(input).join(',') !== 'caseId' || !cases.has(input.caseId)) {
    throw new Error('Select one reviewed local intake case.');
  }
  const directory = await privateDirectory(root, invocation);
  const database = join(directory, 'intake.sqlite');
  const request = join(directory, 'form.json');
  const cli = ['uv', 'run', '--locked', '--no-sync'];
  const env = { ...process.env, CONNECTOR_MODE: 'synthetic', ALLOW_PIPEDREAM_ACTIONS: '0',
    ...(parameters.model ? { GEMINI_MODEL: parameters.model } : {}) };
  try {
    await invokeCLI([...cli, 'lead-intake', '--init-case', input.caseId, '--db', database, '--request-out', request], { cwd: root, signal, env });
    const output = await invokeCLI([...cli, 'python', 'firedrill/request.py', '--db', database, '--request', request],
      { cwd: root, signal, env, timeoutMs: 210000 });
    return { application: output, localState: { directory, databaseFile: database, requestFile: request } };
  } catch (error) {
    if (error instanceof ApplicationError) error.evidence.runDirectory = directory;
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await commandMain(runApplication);
}
