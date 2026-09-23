const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

test('a decision cannot target a finding whose details are still loading', async () => {
  const elements = new Map();
  const element = (selector) => {
    if (!elements.has(selector)) {
      elements.set(selector, {
        value: '', style: {}, classList: { toggle() {} },
        replaceChildren() {}, addEventListener() {},
      });
    }
    return elements.get(selector);
  };
  const requests = [];
  let respond;
  const context = vm.createContext({
    URLSearchParams, location: { hash: '#token=test' },
    window: { require: Object.assign(() => {}, { config() {} }) },
    document: {
      querySelector: element,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    fetch(url, options) {
      requests.push({ url, options });
      return new Promise((resolve) => { respond = resolve; });
    },
    assert,
  });
  vm.runInContext(fs.readFileSync(path.join(
    __dirname, '../src/flexiv_tidy/assets/review_clang_tidy_web.js',
  ), 'utf8'), context);
  // Leave the initial state fetch pending and simulate navigation from a
  // displayed finding to another one on a slow connection.
  requests.length = 0;
  const selection = vm.runInContext(`
    selectedId = 1;
    detail = { finding: { id: 1 }, files: [] };
    select(2);
  `, context);
  await vm.runInContext("decide('accept')", context);
  assert.deepEqual(requests.map((request) => request.url), ['/api/findings/2']);
  vm.runInContext('assert.equal(detail, null)', context);
  respond({
    ok: true,
    json: async () => ({
      finding: { id: 2, status: 'pending', fixable: false },
      files: [], error: '',
    }),
  });
  await selection;
  vm.runInContext('assert.equal(detail.finding.id, 2)', context);
});
