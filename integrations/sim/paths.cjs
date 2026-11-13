const path = require('node:path');
const repo = path.resolve(__dirname, '../..');
const state = path.resolve(process.env.SUSANOO_SIM_STATE_DIR || path.join(repo,'.platform/sim-local'));
const fs = require('node:fs');
function credentialsFile() {
  if(process.env.PLATFORM_BROWSER_CREDENTIALS) return process.env.PLATFORM_BROWSER_CREDENTIALS;
  const files=fs.readdirSync(path.join(repo,'.platform')).filter(name=>/^demo-credentials-.*\.json$/.test(name));
  if(files.length!==1) throw new Error('Set PLATFORM_BROWSER_CREDENTIALS to the intended local synthetic account file');
  return path.join(repo,'.platform',files[0]);
}
module.exports={repo,state,credentialsFile};
