import test from 'node:test';
import assert from 'node:assert/strict';
import { enabledTargets, activityComplete, uploadState, filterActivities, manualImpact, normalizeReport, ledgerSaveState, configCompleteness, summarize, simulationEvidence } from '../web/src/dashboard-model.js';
import { demoReport, demoSnapshot } from '../web/src/dashboard-demo.js';

test('activity completion considers each enabled target and never completes an empty target set', () => {
  const activity = { targets: { CN: { status: 'success' }, GLOBAL: { status: 'auth_required' }, STRAVA: { status: 'uploading' } } };
  assert.equal(activityComplete(activity, ['CN']), true);
  assert.equal(activityComplete(activity, ['CN', 'GLOBAL']), false);
  assert.equal(activityComplete(activity, []), false);
  assert.equal(activityComplete(activity, ['STRAVA']), false);
  assert.deepEqual(enabledTargets(demoSnapshot), ['CN', 'GLOBAL', 'STRAVA']);
  assert.equal(configCompleteness({ secrets: ['GARMIN_CN_OAUTH1'] }).CN, false);
});

test('auth, retry limits, uncertainty and asynchronous uploads keep distinct states', () => {
  assert.equal(uploadState({ status: 'auth_required', attempts: 1, reconciliation_status: 'unverified' }), 'auth_required');
  assert.equal(uploadState({ status: 'auth_required', attempts: 8 }), 'exhausted');
  assert.equal(uploadState({ status: 'failed', reconciliation_status: 'ambiguous' }), 'ambiguous');
  assert.equal(uploadState({ status: 'uploading', reconciliation_status: 'unverified' }), 'uploading');
  assert.equal(uploadState({ status: 'success' }, false), 'disabled');
  assert.equal(uploadState({ status: '__proto__' }), 'unknown');
});

test('filters combine activity query, completion and individual platform state', () => {
  const rows = demoReport.activity_items;
  const targets = enabledTargets(demoSnapshot);
  assert.equal(filterActivities(rows, { tab: 'attention' }, targets).length, 3);
  assert.equal(filterActivities(rows, { tab: 'complete' }, targets).length, 9);
  assert.equal(filterActivities(rows, { target: 'GLOBAL', status: 'auth_required' }, targets).length, 1);
  assert.equal(filterActivities(rows, { query: '周末耐力' }, targets)[0].id, rows[0].id);
  assert.equal(filterActivities(rows, { query: 'DEMO-1025-0001' }, targets).length, 1);
  assert.equal(filterActivities(rows, { days: 7, now: Date.parse('2026-10-26T00:00:00Z') }, targets).length, 7);
  assert.deepEqual(summarize(demoReport, targets), { activities: 12, total: 36, complete: 33, pending: 3 });
});

test('single-run Garmin selection retains independently enabled Strava and global dry-run', () => {
  assert.deepEqual(manualImpact('CN', demoSnapshot), { targets: ['CN', 'STRAVA'], dryRun: false });
  assert.deepEqual(manualImpact('DEFAULT', { ...demoSnapshot, variables: { ...demoSnapshot.variables, SYNC_DRY_RUN: 'true' } }, false), { targets: ['CN', 'GLOBAL', 'STRAVA'], dryRun: true });
});

test('report normalization only exposes allowed metadata and supports aggregate-only older reports', () => {
  const input = structuredClone(demoReport);
  input.source_json = 'secret-source';
  input.activity_items[0].source_json = 'secret-source';
  input.activity_items[0].targets.CN.last_error = 'secret-error';
  const normalized = normalizeReport(input);
  assert.doesNotMatch(JSON.stringify(normalized), /secret-source|secret-error/);
  assert.equal(normalized.activity_items.length, 12);
  assert.deepEqual(normalizeReport({ schema_version: 1, ledger: { uploads: [] } }).activity_items, []);
  assert.throws(() => normalizeReport({ schema_version: 3 }), /格式/);
  assert.deepEqual(normalizeReport({ schema_version: 2, ledger: { uploads: [null] }, run: { targets: 'invalid' }, activity_items: [null] }).run.targets, []);
  input.activity_items = Array.from({ length: 250 }, () => input.activity_items[0]);
  assert.equal(normalizeReport(input).activity_items.length, 200);
});

test('ledger saving uses the actual persistence step, independent of workflow exit status', () => {
  assert.equal(ledgerSaveState([{ conclusion: 'success', steps: [] }]), 'unknown');
  assert.equal(ledgerSaveState([{ steps: [{ name: '保存台账与汇总', conclusion: 'success', status: 'completed' }, { name: '清理', conclusion: 'failure' }] }]), 'saved');
  assert.equal(ledgerSaveState([{ steps: [{ name: '保存台账与汇总', conclusion: 'failure', status: 'completed' }] }]), 'failed');
  assert.equal(ledgerSaveState([{ steps: [{ name: '保存台账与汇总', conclusion: null, status: 'in_progress' }] }]), 'pending');
  assert.equal(ledgerSaveState([{ steps: [{ name: '保存台账与汇总', conclusion: 'skipped', status: 'completed' }] }]), 'unknown');
});


test('setup requires a matching successful single-activity simulation, not a checkbox or unrelated run', () => {
  const report = structuredClone(demoReport);
  const id = report.activity_items[0].id;
  report.run = { ...report.run, id: '123', record_id: id, dry_run: true, status: 'success' };
  const runs = [{ id: 123, status: 'completed', conclusion: 'success' }];
  assert.equal(simulationEvidence(report, runs, id, ['CN', 'GLOBAL']), '123');
  assert.equal(simulationEvidence(report, [], id, ['CN']), '');
  assert.equal(simulationEvidence(report, runs, 'another-record', ['CN']), '');
  assert.equal(simulationEvidence(report, [{ ...runs[0], conclusion: 'failure' }], id, ['CN']), '');
  report.run.dry_run = false;
  assert.equal(simulationEvidence(report, runs, id, ['CN']), '');
  report.run.dry_run = true;
  report.activity_items[0].processing_status = 'failed';
  assert.equal(simulationEvidence(report, runs, id, ['CN']), '');
});
