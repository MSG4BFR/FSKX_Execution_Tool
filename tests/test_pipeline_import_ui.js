/* Exercise the actual import handler with a tiny DOM/fetch stub (no dependencies). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../app/templates/join.html'), 'utf8');
const start = html.indexOf('function importPipeline(input){');
assert.ok(start >= 0);
const end = html.indexOf('\n// ---- bound-port map', start);
assert.ok(end > start);
const source = html.slice(start, end);

async function check(checked, response) {
  const requests = [], messages = [], refreshed = [], loaded = [];
  class Form {
    constructor() { this.fields = new Map(); }
    append(key, value) { this.fields.set(key, value); }
  }
  const context = {
    FormData: Form,
    document: { getElementById(id) { assert.equal(id, 'rejectImportConflicts'); return { checked }; } },
    flash(value) { messages.push(value); },
    refreshSaved(value) { refreshed.push(value); },
    loadRecord(value) { loaded.push(value); },
    fetch(url, options) {
      requests.push({ url, options });
      return Promise.resolve({ json: async () => options ? response : { id: 'new-id' } });
    },
  };
  vm.runInNewContext(source, context);
  const file = { name: 'neutral.fskxp' };
  const input = { files: [file], value: 'neutral.fskxp' };
  context.importPipeline(input);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests[0].url, '/api/pipelines/import');
  assert.equal(requests[0].options.body.fields.get('file'), file);
  assert.equal(requests[0].options.body.fields.get('conflict_policy'), checked ? 'reject' : 'keep_local');
  assert.equal(input.value, '');
  if (response.error) {
    assert.equal(requests.length, 1, 'no retry or workflow load after rejection');
    assert.equal(refreshed.length, 0);
    assert.equal(loaded.length, 0);
    assert.ok(messages[0].includes(response.error));
  } else {
    assert.equal(requests.length, 2);
    assert.equal(refreshed[0], 'new-id');
    assert.equal(loaded[0].id, 'new-id');
  }
}

(async () => {
  await check(false, { id: 'new-id', name: 'Neutral' });
  await check(true, { id: 'new-id', name: 'Neutral' });
  await check(true, { error: 'Conflicting local result: results/A/run-1/outputs.json' });
  console.log('PASS: unchecked keep-local, checked reject, and conflict without retry/workflow replacement');
})().catch(error => { console.error(error); process.exitCode = 1; });
