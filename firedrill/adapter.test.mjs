import test from 'node:test';
import assert from 'node:assert/strict';
import { Readable } from 'node:stream';
import { spawnSync } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TARGET_INVOCATION_VERSION, simulationExitCode } from '@firedrill-run/sdk';
import { applicationEnvironment, invokeCLI, readInvocation, readStdin } from './adapter-runtime.mjs';
import { runApplication } from './target.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const cli = fileURLToPath(new URL('./node_modules/@firedrill-run/cli/bin/firedrill.mjs', import.meta.url));
const envelope = () => ({
  schemaVersion: TARGET_INVOCATION_VERSION,
  runId: 'offline-run', attemptId: 'offline-attempt', scenario: { id: 'ordinary' },
  configuration: { id: 'default', parameters: {} }, tools: {}, input: { caseId: 'ordinary' },
});

test('released 2.2.2 CLI validates the actual command recipe offline', () => {
  const result = spawnSync(process.execPath, [cli, 'validate', 'firedrill/scenario.json', '--json', '--non-interactive'], { cwd: root, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { valid: true, expandedRuns: 4, target: 'command' });
});

test('current invocation accepts task identity without controller data', () => {
  const value = envelope();
  assert.equal(readInvocation(value), value);
});

for (const [name, change] of [
  ['retired invocation version', v => { v.schemaVersion = '2026-10-14'; }],
  ['full controller scenario', v => { v.scenario.checks = []; }],
  ['assessment payload', v => { v.checks = []; }],
  ['unsupported Tool connections', v => { v.tools.mail = { token: 'synthetic' }; }],
  ['unreviewed configuration', v => { v.configuration.parameters.allowWrite = true; }],
  ['invalid step', v => { v.step = { id: 's', ordinal: true }; }],
  ['missing application task', v => { delete v.input; }],
]) test(`rejects ${name} before application execution`, () => {
  const value = envelope(); change(value);
  assert.throws(() => readInvocation(value));
});

test('stdin is bounded and rejects invalid UTF-8 instead of replacing bytes', async () => {
  await assert.rejects(readStdin(Readable.from([Buffer.alloc(128 * 1024 + 1)])));
  await assert.rejects(readStdin(Readable.from([Buffer.from([0xff])])));
  const invocation = await readStdin(Readable.from([Buffer.from(JSON.stringify(envelope()))]));
  assert.equal(invocation.input.caseId, 'ordinary');
});

test('operator/CI authority never reaches the application, while its model key remains separate', () => {
  assert.deepEqual(applicationEnvironment({ FIREDRILL_TOKEN: 'operator', FIREDRILL_INPUT_JSON: 'controller', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'ci', OPENAI_API_KEY: 'model', PATH: '/bin' }), { OPENAI_API_KEY: 'model', PATH: '/bin' });
});

test('actual subprocess output is returned with literal argv and no shell interpretation', async () => {
  const result = await invokeCLI([process.execPath, '-e', 'console.log(JSON.stringify({task:process.argv[1],operator:process.env.FIREDRILL_TOKEN??null}))', '$(never-execute)'], { env: { PATH: process.env.PATH, FIREDRILL_TOKEN: 'operator' } });
  assert.deepEqual(result, { task: '$(never-execute)', operator: null });
});

test('failed application retains original partial output and stderr without returning success', async () => {
  await assert.rejects(invokeCLI([process.execPath, '-e', 'process.stdout.write("partial");process.stderr.write("failed after work");process.exitCode=7']), error => {
    assert.equal(error.evidence.exitCode, 7);
    assert.equal(error.evidence.stdout, 'partial');
    assert.equal(error.evidence.stderr, 'failed after work');
    return true;
  });
});

test('malformed application output cannot be released as structured success', async () => {
  await assert.rejects(invokeCLI([process.execPath, '-e', 'console.log("not JSON")']), /one JSON value/);
});

test('cancellation drains the owned foreground child before failure', async () => {
  const controller = new AbortController();
  const running = invokeCLI([process.execPath, '-e', 'setInterval(()=>{},1000)'], { signal: controller.signal });
  const timer = setTimeout(() => controller.abort(new Error('offline cancellation')), 100);
  try { await assert.rejects(running, error => error.message === 'offline cancellation' && error.evidence.signal === 'SIGTERM'); }
  finally { clearTimeout(timer); }
});

test('unreviewed inputs cannot select files, commands or write grants', async () => {
  await assert.rejects(runApplication({ caseId: 'ordinary', argv: ['sh'] }, {}, undefined));
  await assert.rejects(runApplication({ caseId: '../private' }, {}, undefined));
});

test('actual ASGI intake records a failed classifier attempt without a model key', () => {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (/KEY|TOKEN|CREDENTIAL/.test(key)) delete env[key];
  const result = spawnSync(process.execPath, ['firedrill/target.mjs'], { cwd: root, input: JSON.stringify(envelope()), env, encoding: 'utf8', timeout: 30000 });
  assert.equal(result.status, 1, result.stderr);
  assert.equal(result.stdout, '');
  const failure = JSON.parse(result.stderr);
  assert.equal(failure.application.exitCode, 1);
  const observed = JSON.parse(failure.application.stdout);
  assert.equal(observed.http.status, 502);
  assert.equal(observed.event.body.model_attempts[0].status, 'failed');
  assert.equal(observed.event.body.cost_usd, null);
  assert.equal(observed.appLifespanClosed, true);
});

function invokeCase(caseId, additions = {}) {
  const env = { ...process.env, ...additions };
  for (const key of Object.keys(env)) if (/KEY|TOKEN|CREDENTIAL/.test(key)) delete env[key];
  const value = envelope(); value.input.caseId = caseId;
  return spawnSync(process.execPath, ['firedrill/target.mjs'], { cwd: root, input: JSON.stringify(value), env, encoding: 'utf8', timeout: 30000 });
}

test('actual completed API event replays without a model and with explicit synthetic scope', () => {
  const result = invokeCase('prior-complete', { CONNECTOR_MODE: 'pipedream', ALLOW_PIPEDREAM_ACTIONS: '1' });
  assert.equal(result.status, 0, result.stderr);
  const observed = JSON.parse(result.stdout).application;
  assert.equal(observed.connectorMode, 'synthetic');
  assert.equal(observed.http.status, 200);
  assert.equal(observed.http.body.replayed, true);
  assert.equal(observed.http.body.classifier_called_this_request, false);
  assert.deepEqual(observed.event.body.model_attempts, []);
  assert.equal(observed.appLifespanClosed, true);
});

test('actual uncertain notification is retained as review without an automatic retry', () => {
  const result = invokeCase('uncertain-notice');
  assert.equal(result.status, 0, result.stderr);
  const observed = JSON.parse(result.stdout).application;
  assert.equal(observed.http.status, 409);
  assert.equal(observed.event.body.stage, 'notification_started');
  assert.deepEqual(observed.event.body.model_attempts, []);
  assert.deepEqual(observed.event.body.action_attempts, []);
  assert.equal(observed.event.body.cost_usd, null);
});

test('failed API invocations retain separate private SQLite state and outer execution identity', async () => {
  const first = invokeCase('ordinary');
  assert.equal(first.status, 1, first.stderr);
  const directory = JSON.parse(first.stderr).application.runDirectory;
  const database = join(directory, 'intake.sqlite');
  const before = await readFile(database);
  const second = invokeCase('ordinary');
  assert.equal(second.status, 1, second.stderr);
  assert.notEqual(JSON.parse(second.stderr).application.runDirectory, directory);
  assert.deepEqual(await readFile(database), before);
  assert.equal((await stat(database)).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(await readFile(join(directory, 'invocation.json'))), {
    source: 'command_adapter', identity: { runId: 'offline-run', attemptId: 'offline-attempt', scenarioId: 'ordinary' },
  });
});

test('SDK result status keeps pending work, unknown execution and gates distinct', () => {
  const value = { batch: { total: 1, counts: { completed: 1, error: 0, interrupted: 0, cancelled: 0, running: 0, queued: 0 } }, runs: [], gate: 'not_configured' };
  assert.equal(simulationExitCode(value), 0);
  assert.equal(simulationExitCode({ ...value, gate: 'inconclusive' }), 1);
  assert.equal(simulationExitCode({ ...value, batch: { total: 2, counts: value.batch.counts } }), 5);
  assert.equal(simulationExitCode({ ...value, runs: [{ assessment: 'pending' }] }), 6);
});

test('runner help never initializes scope or starts a simulation', () => {
  const result = spawnSync(process.execPath, ['firedrill/simulate.mjs', '--help'], { cwd: root, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--resume OPERATION_ID/);
});

test('reviewed recipe stays sequential, bounded and free of credentials or evaluation answers', async () => {
  const value = JSON.parse(await readFile(new URL('./scenario.json', import.meta.url)));
  assert.equal(value.concurrency, 1);
  assert.equal(value.limits.maxRuns, value.scenarios.length);
  assert.equal(value.services, undefined);
  assert.equal(value.gate, undefined);
  for (const scenario of value.scenarios) assert.deepEqual(Object.keys(scenario.input), ['caseId']);
});
