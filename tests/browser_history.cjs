/* Optional real-Chrome UI test. Uses the same Playwright setup as browser_mcp.cjs. */
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const path=require('node:path');
const {chromium}=require('playwright');
async function main(){
 const fixture=spawn(process.env.BILI_TEST_PYTHON||'python3',[path.join(__dirname,'browser_history_fixture.py')],{stdio:['ignore','pipe','inherit']});
 let browser;
 try{
  const info=await new Promise((resolve,reject)=>{let buf='';fixture.stdout.on('data',d=>{buf+=d;if(buf.includes('\n'))resolve(JSON.parse(buf.split('\n')[0]))});fixture.once('exit',()=>reject(Error('fixture exited')))});
  browser=await chromium.launch({channel:'chrome',headless:true});const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(info.url);assert.equal(await page.title(),'b站视频插件');
  await page.locator('#token').fill('ephemeral-history-browser-token-123456');await page.locator('#login-form button').click();
  await page.locator('[data-tab="tasks"]').click();
  await page.locator(`[data-action="task-logs"][data-id="${info.first}"]`).click();
  await page.locator('#modal').getByText('HTTP 403：',{exact:false}).waitFor();
  await page.locator('#modal [data-action="history-logs"]').click();await page.locator('#history-confirm').click();
  await page.locator('#modal').waitFor({state:'hidden'});
  await page.locator(`[data-action="task-logs"][data-id="${info.first}"]`).click();
  await page.locator('#modal').getByText('暂无诊断日志。',{exact:false}).waitFor();
  await page.locator('#modal .modal-foot [data-action="close"]').click();
  await page.locator(`[data-action="task-retry"][data-id="${info.first}"]`).click();
  await page.locator(`[data-action="task-cancel"][data-id="${info.first}"]`).waitFor();
  assert.equal(await page.locator('tbody tr').first().locator('.small-id').innerText(),info.first);
  await page.locator(`[data-action="history-tasks"][data-id="${info.second}"]`).click();
  await page.locator('#history-confirm').click();await page.locator('#modal').waitFor({state:'hidden'});
  await page.locator(`[data-action="task-retry"][data-id="${info.second}"]`).waitFor({state:'detached'});
  await page.getByRole('button',{name:'清除已结束记录',exact:true}).click();await page.locator('#history-confirm').click();
  await page.locator('#modal').waitFor({state:'hidden'});
  assert.equal(await page.locator(`[data-action="task-cancel"][data-id="${info.first}"]`).count(),1);
  await page.screenshot({path:'/tmp/bili-history-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:'/tmp/bili-history-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);process.stdout.write('History browser: branding, logs, deletion, retry sorting, active protection passed\n');
 }finally{if(browser)await browser.close();fixture.kill('SIGTERM')}
}
main().catch(e=>{console.error(e);process.exitCode=1});
