const {repo,state,credentialsFile} = require('./paths.cjs');
const {chromium} = require(require('node:path').join(repo,'tests/e2e/node_modules/@playwright/test'));
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}});
    const account = JSON.parse(fs.readFileSync(path.join(state, '.private/account.json')));
    const binding = JSON.parse(fs.readFileSync(path.join(state, '.private/binding.json')));
    const auth = await context.request.post('http://localhost:3010/api/auth/sign-in/email', {
      data: account, headers: {Origin: 'http://localhost:3010'},
    });
    if (!auth.ok()) throw new Error(`Login failed: ${auth.status()}`);
    const page = await context.newPage();
    await page.goto(`http://localhost:3010/workspace/${binding.workspace_id}/w/${binding.workflow_id}`);
    await page.getByText('Plan Synthetic Promise', {exact: true}).first().waitFor({timeout: 60000});
    await page.screenshot({path: path.join(state, 'local-workflow.png'), fullPage: true});
    console.log('PASS: authenticated Sim workflow canvas renders');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
