"""Optional Node unit harness for the real key handler; not browser visual QA."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Optional Node JS test runtime unavailable"
)
def test_chat_enter_shift_enter_composition_and_busy_guards():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const handlers = {};
let submitted = 0, busy = false;
const composer = { getAttribute: () => String(busy), requestSubmit: () => submitted++ };
const document = {
  addEventListener: (name, handler) => handlers[name] = handler,
  querySelectorAll: () => [],
  getElementById: (id) => id === 'composer' ? composer : null,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document});
function check(overrides, sends, prevents, waiting = false) {
  submitted = 0; busy = waiting;
  let prevented = false;
  const event = {target: {id:'message'}, key:'Enter', shiftKey:false,
    isComposing:false, keyCode:13, repeat:false,
    preventDefault: () => prevented = true, ...overrides};
  handlers.keydown(event);
  assert.equal(submitted, sends);
  assert.equal(prevented, prevents);
}
check({}, 1, true);
check({shiftKey:true}, 0, false);
check({isComposing:true}, 0, false);
check({keyCode:229}, 0, false);
check({repeat:true}, 0, true);
check({}, 0, true, true);
check({key:'a'}, 0, false);
check({target:{id:'other'}}, 0, false);
check({ctrlKey:true}, 1, true);
"""
    asset = Path(__file__).parents[1] / "src/solakit/ui/static/app.js"
    subprocess.run([shutil.which("node"), "-e", script, str(asset)], check=True)
