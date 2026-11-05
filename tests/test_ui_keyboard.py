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


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Optional Node JS test runtime unavailable"
)
def test_stream_client_submission_snapshots_disconnect_and_stale_events():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
(async () => {
  const handlers = {}, sources = [];
  let posts = 0;
  const field = {value:'hello', disabled:false, style:{},
    scrollHeight:40, focus:()=>{}};
  const button = {disabled:false};
  const composer = {action:'/sessions/s/messages', busy:'false', dataset:{},
    getAttribute:()=>composer.busy,
    setAttribute:(k,v)=>composer.busy=v,
    querySelector:()=>field, querySelectorAll:()=>[button]};
  const conversation = {dataset:{eventsUrl:'/sessions/s/events'},
    innerHTML:'settled history', querySelector:()=>null,
    scrollTop:0, scrollHeight:1000, clientHeight:200,
    querySelectorAll:()=>[]};
  const nodes = {composer, conversation, pending:{hidden:true},
    elapsed:{textContent:''}, 'connection-error':{hidden:true}};
  const document = {addEventListener:(k,v)=>handlers[k]=v,
    querySelectorAll:()=>[], getElementById:k=>nodes[k],
    createElement:()=>({content:{querySelector:()=>null}})};
  class EventSource {
    constructor(url) {this.url=url;this.handlers={};sources.push(this);}
    addEventListener(k,v) {this.handlers[k]=v;}
    close() {this.closed=true;}
  }
  vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'), {
    document, EventSource, URLSearchParams,
    setInterval:()=>1, clearInterval:()=>{},
    fetch:async (url, options)=>{
      posts++; assert.equal(String(options.body),'message=hello');
      assert.equal(options.headers.Accept,'application/json');
      return {status:202, json:async()=>({run_id:"r",
        created_at:new Date(Date.now()-65000).toISOString()})};
    },
  });
  conversation.scrollTop=0; // Reading earlier content.
  const submit = ()=>handlers.submit({target:composer,preventDefault:()=>{}});
  await submit();
  assert.equal(posts,1); assert.equal(field.value,'');
  assert.equal(field.disabled,false); assert.equal(button.disabled,true);
  assert.match(nodes.elapsed.textContent,/1:0[5-9]/);
  field.value="next draft";
  await submit(); assert.equal(posts,1); // Busy guard.
  sources[0].handlers.snapshot({data:JSON.stringify({html:'live',pending:true})});
  assert.equal(conversation.innerHTML,'settled history');
  assert.equal(conversation.scrollTop,0);
  sources[0].handlers.snapshot({data:JSON.stringify({html:'saved',pending:false})});
  assert.equal(field.disabled,false); assert.equal(sources[0].closed,true);
  assert.equal(field.value,"next draft");
  field.value='hello'; await submit(); assert.equal(posts,2);
  sources[0].onerror(); // A stale stream must not close the current one.
  assert.equal(sources[1].closed,undefined);
  sources[1].onerror();
  assert.equal(sources[1].closed,true); assert.equal(field.disabled,false);
  assert.equal(nodes['connection-error'].hidden,false); assert.equal(posts,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    asset = Path(__file__).parents[1] / "src/solakit/ui/static/app.js"
    subprocess.run([shutil.which("node"), "-e", script, str(asset)], check=True)
