/* Optional real-Chrome regression. Requires playwright and an installed Chrome.
 * NODE_PATH=/path/to/node_modules BILI_TEST_PYTHON=/path/to/python node tests/browser_mcp.cjs
 * Uses only ephemeral loopback services; no Bilibili or production credentials.
 */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createHash} = require('node:crypto');
const https = require('node:https');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');
let stage='fixture startup';

async function main() {
  const fixture = spawn(process.env.BILI_TEST_PYTHON || 'python3', [path.join(__dirname,'browser_oauth_fixture.py')], {stdio:['ignore','pipe','pipe']});
  let browser, callbackServer;
  try {
    const info = await new Promise((resolve,reject) => {
      let buffer='';
      fixture.stdout.on('data', data => {
        buffer += data;
        if (buffer.includes('\n')) resolve(JSON.parse(buffer.split('\n')[0]));
      });
      fixture.once('exit', () => reject(new Error('HTTPS fixture exited')));
    });
    const callbackRequests=[];
    callbackServer=http.createServer((req,res) => {
      const url=new URL(req.url,'http://127.0.0.1');
      if (url.pathname==='/callback') callbackRequests.push(url);
      res.end('OAuth callback received');
    });
    await new Promise(resolve=>callbackServer.listen(0,'127.0.0.1',resolve));
    const redirect='http://127.0.0.1:'+callbackServer.address().port+'/callback';
    async function post(endpoint,data,form=false) {
      const payload=form?new URLSearchParams(data).toString():JSON.stringify(data);
      return await new Promise((resolve,reject) => {
        const req=https.request(info.origin+endpoint,{method:'POST',rejectUnauthorized:false,headers:{
          'Content-Type':form?'application/x-www-form-urlencoded':'application/json','Content-Length':Buffer.byteLength(payload)
        }},res=>{
          let raw='';res.on('data',d=>raw+=d);res.on('end',()=>{
            try { resolve({status:res.statusCode,data:JSON.parse(raw)}); } catch { reject(new Error('Invalid fixture response')); }
          });
        });
        req.on('error',reject);req.end(payload);
      });
    }
    stage='registration';
    const registered=await post('/oauth/register',{redirect_uris:[redirect],client_name:'real-browser-test'});
    assert.equal(registered.status,201);
    stage='Chrome startup';browser=await chromium.launch({channel:'chrome',headless:true});
    const verifier='v'.repeat(43);
    const challenge=createHash('sha256').update(verifier).digest('base64url');
    async function authorization(context,state) {
      const page=await context.newPage();
      const query=new URLSearchParams({client_id:registered.data.client_id,redirect_uri:redirect,response_type:'code',
        resource:info.origin+'/mcp',scope:'library',state,code_challenge:challenge,code_challenge_method:'S256'});
      await page.goto(info.origin+'/oauth/authorize?'+query);
      await page.locator('input[name="owner"]').fill(info.owner);
      return page;
    }
    // Exercise the old 303 branch too: browsers differ in redirect CSP enforcement.
    const old=await browser.newContext({ignoreHTTPSErrors:true,extraHTTPHeaders:{Accept:'application/json'}});
    stage='old authorization page';const broken=await authorization(old,'old-policy-repro');
    let blocked=false;
    broken.on('console',msg=>{if(/form-action/.test(msg.text())) blocked=true;});
    stage='old form submission';await broken.locator('button[value="yes"]').click({noWaitAfter:true});
    await broken.waitForTimeout(1000);
    console.log('Old-flow diagnostics: callbacks='+callbackRequests.length+', CSP blocked='+blocked);
    if (callbackRequests.length===0 && !blocked) {
      const text=await broken.locator('body').innerText();
      if (text.startsWith('{')) {
        const error=JSON.parse(text);console.log('Old-flow rejection: '+(error.code||error.error));
      }
    }
    stage='old callback CSP assertion';assert.ok(callbackRequests.length===1 || blocked,'old flow neither completed nor reported CSP block');
    await old.close();
    console.log('PASS: old redirect baseline observed (browser-dependent CSP behavior)');
    callbackRequests.length=0;

    const context=await browser.newContext({ignoreHTTPSErrors:true});
    stage='fixed authorization page';const page=await authorization(context,'fixed-flow');
    const policy=(await page.request.get(info.origin+'/oauth/complete.js')).headers();
    assert.match(policy['content-security-policy'],/form-action 'self'/);
    // Native HTML form, real cookies, Origin header, TLS and callback navigation.
    stage='fixed callback navigation';await Promise.all([page.waitForURL(redirect+'?**',{timeout:15000}),page.locator('button[value="yes"]').click()]);
    assert.equal(callbackRequests.length,1);
    const callback=callbackRequests[0];
    assert.equal(callback.searchParams.get('state'),'fixed-flow');
    stage='token exchange';const exchanged=await post('/oauth/token',{grant_type:'authorization_code',client_id:registered.data.client_id,
      redirect_uri:redirect,resource:info.origin+'/mcp',code:callback.searchParams.get('code'),code_verifier:verifier},true);
    assert.equal(exchanged.status,200);
    assert.equal(exchanged.data.token_type,'Bearer');
    await context.close();
    console.log('PASS: real Chrome native form -> completion page -> loopback callback -> token exchange');
  } finally {
    if (browser) await browser.close();
    if (callbackServer) await new Promise(resolve=>callbackServer.close(resolve));
    fixture.kill('SIGTERM');
  }
}
main().catch(error=>{console.error('Browser OAuth regression failed at '+stage+' ('+error.name+'; credentials omitted)');process.exitCode=1;});
