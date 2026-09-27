import { Storage } from 'megajs';
import 'dotenv/config';
import fs from 'node:fs';
import path from 'node:path';

const ROOT_DIR = process.cwd();
const CONFIG_FILE = path.join(ROOT_DIR, 'config.json');
const STATE_FILE = path.join(ROOT_DIR, 'bill_state.json');
const EMAIL = process.env.MEGA_EMAIL;
const PASSWORD = process.env.MEGA_PASSWORD;

class SyncFailure extends Error {}

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseJson(input, label) {
  try {
    return JSON.parse(Buffer.isBuffer(input) ? input.toString('utf8') : input);
  } catch {
    throw new SyncFailure(`${label} is not valid JSON.`);
  }
}

function parseConfig(input) {
  const value = parseJson(input, 'config.json');
  if (!isRecord(value)) throw new SyncFailure('config.json must be a JSON object.');
  for (const key of ['iesco', 'sngpl']) {
    if (value[key] !== undefined && !Array.isArray(value[key])) {
      throw new SyncFailure(`config.json field ${key} must be an array.`);
    }
  }
  if (value.ntfy_key !== undefined && typeof value.ntfy_key !== 'string') {
    throw new SyncFailure('config.json field ntfy_key must be a string.');
  }
  if (value.iesco === undefined && value.sngpl === undefined) {
    throw new SyncFailure('config.json must contain an iesco or sngpl array.');
  }
  return value;
}

function parseState(input) {
  const value = parseJson(input, 'bill_state.json');
  if (!isRecord(value)) throw new SyncFailure('bill_state.json must be a JSON object.');
  return value;
}

function writePrivateFile(filePath, data) {
  const temporaryPath = `${filePath}.${process.pid}.tmp`;
  fs.writeFileSync(temporaryPath, data, { mode: 0o600 });
  fs.renameSync(temporaryPath, filePath);
  fs.chmodSync(filePath, 0o600);
}

function newestFile(folder, name) {
  const matches = (folder.children ?? []).filter((node) => !node.directory && node.name === name);
  return matches.reduce((newest, node) => {
    if (!newest) return node;
    return Number(node.timestamp ?? 0) >= Number(newest.timestamp ?? 0) ? node : newest;
  }, undefined);
}

function findFolder(folder, name) {
  return (folder.children ?? []).find((node) => node.directory && node.name === name);
}

async function getBillFolder(storage, create) {
  let rootData = findFolder(storage.root, 'github-data');
  if (!rootData && create) rootData = await storage.root.mkdir('github-data');
  if (!rootData) return undefined;

  let billFolder = findFolder(rootData, 'bill-checker');
  if (!billFolder && create) billFolder = await rootData.mkdir('bill-checker');
  return billFolder;
}

async function openStorage() {
  if (!EMAIL || !PASSWORD) {
    throw new SyncFailure('MEGA_EMAIL and MEGA_PASSWORD Actions secrets are required.');
  }
  const storage = await new Storage({ email: EMAIL, password: PASSWORD }).ready;
  await storage.reload(true);
  return storage;
}

async function downloadRemoteFile(folder, name) {
  const node = newestFile(folder, name);
  return node ? node.downloadBuffer({}) : undefined;
}

async function uploadRemoteFile(folder, name, data) {
  const previousFiles = (folder.children ?? []).filter((node) => !node.directory && node.name === name);
  const uploaded = await folder.upload({ name, size: data.length }, data).complete;
  for (const previous of previousFiles) {
    if (previous === uploaded) continue;
    try {
      await previous.delete();
    } catch {
      console.warn(`[MEGA] ${name} uploaded; an older duplicate could not be removed.`);
    }
  }
}

function legacyConfig() {
  const raw = process.env.BILL_REFS?.trim();
  if (!raw) return undefined;
  return parseConfig(raw);
}

function sngplConfig() {
  const raw = process.env.SNGPL_REFS?.trim();
  if (!raw) return undefined;
  const value = parseJson(raw, 'SNGPL_REFS');
  const accounts = Array.isArray(value) ? value : value.sngpl;
  if (!Array.isArray(accounts)) throw new SyncFailure('SNGPL_REFS must contain an array of SNGPL accounts.');
  return { sngpl: accounts };
}

function normalizeConfig(config) {
  const normalized = {
    iesco: Array.isArray(config.iesco) ? config.iesco : [],
    sngpl: Array.isArray(config.sngpl) ? config.sngpl : [],
  };
  if (typeof config.ntfy_key === 'string') normalized.ntfy_key = config.ntfy_key;
  return normalized;
}

function accountKey(account, type) {
  const reference = account.ref ?? account.consumer ?? '';
  return `${type}:${account.name ?? ''}:${reference}`;
}

function mergeAccounts(existing, incoming, type) {
  const merged = [...(existing ?? [])];
  const seen = new Set(merged.map((account) => accountKey(account, type)));
  for (const account of incoming ?? []) {
    const key = accountKey(account, type);
    if (!seen.has(key)) {
      merged.push(account);
      seen.add(key);
    }
  }
  return merged;
}

function referenceGuide(config) {
  const lines = [
    'UTILITY BILL REFERENCE NUMBERS',
    '================================',
    '',
    'IESCO REFERENCE NUMBERS',
    '-----------------------',
  ];
  for (const account of config.iesco) {
    lines.push(`${account.name ?? 'IESCO'}: ${account.ref ?? account.consumer ?? '(missing reference)'}`);
  }
  if (!config.iesco.length) lines.push('(none configured)');
  lines.push('', 'SNGPL REFERENCE NUMBERS', '-----------------------');
  for (const account of config.sngpl) {
    lines.push(`${account.name ?? 'SNGPL'}: ${account.consumer ?? account.ref ?? '(missing reference)'}`);
  }
  if (!config.sngpl.length) lines.push('(none configured)');
  lines.push('', 'This private guide is generated from config.json.', '');
  return `${lines.join('\n')}\n`;
}

async function syncFromMega() {
  let storage;
  try {
    storage = await openStorage();
    const folder = await getBillFolder(storage, false);
    if (!folder) throw new SyncFailure('MEGA folder github-data/bill-checker was not found.');

    const remoteConfig = await downloadRemoteFile(folder, 'config.json');
    const configData = remoteConfig ?? Buffer.from(JSON.stringify(legacyConfig(), null, 2) + '\n', 'utf8');
    parseConfig(configData);

    const remoteState = await downloadRemoteFile(folder, 'bill_state.json');
    const stateData = remoteState ?? Buffer.from('{}\n', 'utf8');
    parseState(stateData);

    writePrivateFile(CONFIG_FILE, configData);
    writePrivateFile(STATE_FILE, stateData);
    console.log(remoteConfig
      ? '[MEGA] Restored config.json and bill_state.json from the private MEGA folder.'
      : '[MEGA] Restored config.json from BILL_REFS for one-time migration; state initialized locally.');
    return true;
  } catch (error) {
    console.error(`[MEGA] Download failed: ${error instanceof SyncFailure ? error.message : 'MEGA service, account, or network error.'}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

async function syncToMega() {
  let storage;
  try {
    if (!fs.existsSync(CONFIG_FILE)) throw new SyncFailure('Local config.json is missing.');
    if (!fs.existsSync(STATE_FILE)) throw new SyncFailure('Local bill_state.json is missing.');
    const configData = fs.readFileSync(CONFIG_FILE);
    const stateData = fs.readFileSync(STATE_FILE);
    const config = parseConfig(configData);
    parseState(stateData);

    storage = await openStorage();
    const folder = await getBillFolder(storage, true);
    if (!folder) throw new SyncFailure('Could not create MEGA folder github-data/bill-checker.');
    await uploadRemoteFile(folder, 'config.json', configData);
    await uploadRemoteFile(folder, 'bill_state.json', stateData);
    await uploadRemoteFile(folder, 'REFERENCE_NUMBERS.txt', Buffer.from(referenceGuide(normalizeConfig(config)), 'utf8'));
    console.log('[MEGA] Uploaded validated config.json, bill_state.json, and labeled REFERENCE_NUMBERS.txt.');
    return true;
  } catch (error) {
    console.error(`[MEGA] Upload failed: ${error instanceof SyncFailure ? error.message : 'MEGA service, account, or network error.'}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

async function initializeMega() {
  let storage;
  try {
    const config = legacyConfig();
    if (!config) throw new SyncFailure('BILL_REFS is required to initialize the private Mega configuration.');
    storage = await openStorage();
    const folder = await getBillFolder(storage, true);
    if (!folder) throw new SyncFailure('Could not create MEGA folder github-data/bill-checker.');
    const normalized = normalizeConfig(config);
    if (newestFile(folder, 'config.json')) {
      console.log('[MEGA] Existing config.json found; preserved without modification.');
    } else {
      await uploadRemoteFile(folder, 'config.json', Buffer.from(JSON.stringify(normalized, null, 2) + '\n', 'utf8'));
      console.log('[MEGA] Initialized config.json from BILL_REFS.');
    }
    if (newestFile(folder, 'bill_state.json')) {
      console.log('[MEGA] Existing bill_state.json found; preserved without modification.');
    } else {
      await uploadRemoteFile(folder, 'bill_state.json', Buffer.from('{}\n', 'utf8'));
      console.log('[MEGA] Initialized empty bill_state.json.');
    }
    await uploadRemoteFile(folder, 'REFERENCE_NUMBERS.txt', Buffer.from(referenceGuide(normalized), 'utf8'));
    console.log(`[MEGA] Wrote labeled guide: ${normalized.iesco.length} IESCO and ${normalized.sngpl.length} SNGPL reference entries.`);
    return true;
  } catch (error) {
    console.error(`[MEGA] Initialization failed: ${error instanceof SyncFailure ? error.message : 'MEGA service, account, or network error.'}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

async function migrateMega() {
  let storage;
  try {
    const incoming = legacyConfig() ?? {};
    const suppliedSngpl = sngplConfig() ?? {};
    if (!Object.keys(incoming).length && !Object.keys(suppliedSngpl).length) {
      throw new SyncFailure('BILL_REFS or SNGPL_REFS is required to migrate IESCO/SNGPL references.');
    }
    storage = await openStorage();
    const folder = await getBillFolder(storage, false);
    if (!folder) throw new SyncFailure('The private MEGA folder github-data/bill-checker was not found.');
    const remoteConfigData = await downloadRemoteFile(folder, 'config.json');
    const remoteConfig = remoteConfigData ? parseConfig(remoteConfigData) : {};
    const merged = normalizeConfig(remoteConfig);
    merged.iesco = mergeAccounts(merged.iesco, incoming.iesco, 'iesco');
    merged.sngpl = mergeAccounts(merged.sngpl, incoming.sngpl, 'sngpl');
    merged.sngpl = mergeAccounts(merged.sngpl, suppliedSngpl.sngpl, 'sngpl');
    if (typeof incoming.ntfy_key === 'string' && !merged.ntfy_key) merged.ntfy_key = incoming.ntfy_key;
    const configData = Buffer.from(JSON.stringify(merged, null, 2) + '\n', 'utf8');
    await uploadRemoteFile(folder, 'config.json', configData);
    await uploadRemoteFile(folder, 'REFERENCE_NUMBERS.txt', Buffer.from(referenceGuide(merged), 'utf8'));
    console.log(`[MEGA] Migration complete: ${merged.iesco.length} IESCO and ${merged.sngpl.length} SNGPL reference entries are now separated and labeled.`);
    if (!merged.sngpl.length) throw new SyncFailure('No SNGPL references were found in Mega, BILL_REFS, or SNGPL_REFS.');
    return true;
  } catch (error) {
    console.error(`[MEGA] Migration failed: ${error instanceof SyncFailure ? error.message : 'MEGA service, account, or network error.'}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

async function verifyMega() {
  let storage;
  try {
    storage = await openStorage();
    const folder = await getBillFolder(storage, false);
    if (!folder) throw new SyncFailure('The private MEGA bill-checker folder is missing.');
    const configData = await downloadRemoteFile(folder, 'config.json');
    const stateData = await downloadRemoteFile(folder, 'bill_state.json');
    const guide = await downloadRemoteFile(folder, 'REFERENCE_NUMBERS.txt');
    if (!configData) throw new SyncFailure('config.json is missing.');
    if (!stateData) throw new SyncFailure('bill_state.json is missing.');
    if (!guide) throw new SyncFailure('REFERENCE_NUMBERS.txt is missing.');
    const config = normalizeConfig(parseConfig(configData));
    const state = parseState(stateData);
    console.log('[MEGA-VERIFY] Authentication succeeded; private files are present and valid.');
    console.log(`[MEGA-VERIFY] config.json: valid (${configData.length} bytes; ${config.iesco.length} IESCO and ${config.sngpl.length} SNGPL accounts).`);
    console.log(`[MEGA-VERIFY] bill_state.json: valid (${stateData.length} bytes; ${Object.keys(state).length} saved bill records).`);
    console.log(`[MEGA-VERIFY] REFERENCE_NUMBERS.txt: present (${guide.length} bytes; labeled IESCO/SNGPL guide).`);
    return true;
  } catch (error) {
    console.error(`[MEGA-VERIFY] ${error instanceof SyncFailure ? error.message : 'MEGA login, storage access, or file reading failed.'}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

const command = process.argv[2];
const task = command === 'download' ? syncFromMega()
  : command === 'upload' ? syncToMega()
    : command === 'initialize' ? initializeMega()
      : command === 'migrate' ? migrateMega()
        : command === 'verify' ? verifyMega()
          : Promise.reject(new Error('Usage: node mega_sync.mjs <download|upload|initialize|migrate|verify>'));

task.then((ok) => { if (!ok) process.exitCode = 1; }).catch((error) => {
  console.error(`[MEGA] ${error.message}`);
  process.exitCode = 1;
});
