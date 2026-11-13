const {repo,state,credentialsFile} = require('./paths.cjs');
const {chromium, expect} = require(require('node:path').join(repo,'tests/e2e/node_modules/@playwright/test'));
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const report = JSON.parse(fs.readFileSync(path.join(state,'platform-report.json')));
    const accounts = JSON.parse(fs.readFileSync(credentialsFile())).synthetic_local_accounts;
    const owner = accounts.find(a => a.email === 'owner@susanoo.local');
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto('http://localhost:3000/evaluation');
    await page.getByLabel('Email',{exact:true}).fill(owner.email);
    await page.getByLabel('Password',{exact:true}).fill(owner.password);
    await page.getByRole('button',{name:'Sign In',exact:true}).click();
    await page.getByLabel('Active project').selectOption(report.project_id);
    await page.getByRole('button',{name:'Runs',exact:true}).click();
    const row = page.getByRole('row').filter({hasText: report.scenarios[0].run_id.slice(0,8)});
    await expect(row).toContainText(/READY FOR ACCEPTANCE/i,{timeout:30000});
    await expect(row).toContainText('20/20');
    await row.getByRole('button').first().click();
    await expect(page.getByRole('heading',{name:'Run Evidence',exact:true})).toBeVisible();
    await expect(page.getByText('Loading reports...', {exact:true})).toHaveCount(0,{timeout:30000});
    await page.screenshot({path:path.join(state,'platform-twenty-case.png'),fullPage:true});
    const platformState = JSON.parse(fs.readFileSync(path.join(state,'.private/platform.json')));
    const versions = Object.values(platformState.versions);
    const revision = logical => versions.find(v=>v.logical_id===logical && v.approved).id;
    const csrfResponse = await page.request.get('http://localhost:3000/api/v1/auth/csrf');
    const csrf = (await csrfResponse.json()).csrf_token;
    const launched = await page.request.post(`http://localhost:3000/api/v1/projects/${report.project_id}/runs`, {
      headers:{Origin:'http://localhost:3000','X-CSRF-Token':csrf,'Idempotency-Key':randomUUID()},
      data:{suite_version_id:revision('single-case-negative-control-suite'),
        connection_version_id:revision('sim-correct'),environment_version_id:revision('local-simulator')},
    });
    if(!launched.ok()) throw new Error(`Worker test launch refused: ${launched.status()} ${await launched.text()}`);
    const run = await launched.json();
    let stored;
    await expect.poll(async()=>{
      const response=await page.request.get(`http://localhost:3000/api/v1/runs/${run.id}`);
      if(!response.ok()) throw new Error('Unable to read worker result');
      stored=await response.json();
      return stored.gate;
    },{timeout:120000,intervals:[1000,2000]}).toBe('READY_FOR_ACCEPTANCE');
    if(stored.counts.pass!==1 || stored.counts.scheduled!==1) throw new Error('Worker did not execute exactly one case');
    fs.writeFileSync(path.join(state,'worker-check.json'),JSON.stringify({run_id:run.id,gate:stored.gate,counts:stored.counts},null,2));
    await page.reload();
    await page.getByLabel('Active project').selectOption(report.project_id);
    await page.getByRole('button',{name:'Runs',exact:true}).click();
    const workerRow=page.getByRole('row').filter({hasText:run.id.slice(0,8)});
    await expect(workerRow).toContainText(/READY FOR ACCEPTANCE/i,{timeout:30000});
    await workerRow.getByRole('button').first().click();
    await expect(page.getByRole('heading',{name:'Run Evidence',exact:true})).toBeVisible();
    await expect(page.getByText('Loading reports...', {exact:true})).toHaveCount(0,{timeout:30000});
    await page.screenshot({path:path.join(state,'platform-worker-result.png'),fullPage:true});
    if(errors.length) throw new Error(errors.join('; '));
    console.log('PASS: twenty-case console result and newly queued run processed by persistent Sim worker');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
