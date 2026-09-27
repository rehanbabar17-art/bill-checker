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
  parseConfig(raw);
  return Buffer.from(raw, 'utf8');
}

async function syncFromMega() {
  let storage;
  try {
    storage = await openStorage();
    const folder = await getBillFolder(storage, false);
    if (!folder) throw new SyncFailure('MEGA folder github-data/bill-checker was not found.');

    const remoteConfig = await downloadRemoteFile(folder, 'config.json');
    const configData = remoteConfig ?? legacyConfig();
    if (!configData) {
      throw new SyncFailure('MEGA config.json is missing and BILL_REFS was not provided for migration.');
    }
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
    parseConfig(configData);
    parseState(stateData);

    storage = await openStorage();
    const folder = await getBillFolder(storage, true);
    if (!folder) throw new SyncFailure('Could not create MEGA folder github-data/bill-checker.');
    await uploadRemoteFile(folder, 'config.json', configData);
    await uploadRemoteFile(folder, 'bill_state.json', stateData);
    console.log('[MEGA] Uploaded validated config.json and bill_state.json.');
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
    const configData = legacyConfig();
    if (!configData) throw new SyncFailure('BILL_REFS is required to initialize the private Mega configuration.');
    storage = await openStorage();
    const folder = await getBillFolder(storage, true);
    if (!folder) throw new SyncFailure('Could not create MEGA folder github-data/bill-checker.');
    if (newestFile(folder, 'config.json')) {
      console.log('[MEGA] Existing config.json found; preserved without modification.');
    } else {
      await uploadRemoteFile(folder, 'config.json', configData);
      console.log('[MEGA] Initialized config.json from BILL_REFS.');
    }
    if (newestFile(folder, 'bill_state.json')) {
      console.log('[MEGA] Existing bill_state.json found; preserved without modification.');
    } else {
      await uploadRemoteFile(folder, 'bill_state.json', Buffer.from('{}\n', 'utf8'));
      console.log('[MEGA] Initialized empty bill_state.json.');
    }
    return true;
  } catch (error) {
    console.error(`[MEGA] Initialization failed: ${error instanceof SyncFailure ? error.message : 'MEGA service, account, or network error.'}`);
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
    if (!configData) throw new SyncFailure('config.json is missing.');
    if (!stateData) throw new SyncFailure('bill_state.json is missing.');
    const config = parseConfig(configData);
    const state = parseState(stateData);
    console.log('[MEGA-VERIFY] Authentication succeeded; private files are present and valid.');
    console.log(`[MEGA-VERIFY] config.json: valid (${configData.length} bytes; ${(config.iesco ?? []).length} IESCO and ${(config.sngpl ?? []).length} SNGPL accounts).`);
    console.log(`[MEGA-VERIFY] bill_state.json: valid (${stateData.length} bytes; ${Object.keys(state).length} saved bill records).`);
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
      : command === 'verify' ? verifyMega()
        : Promise.reject(new Error('Usage: node mega_sync.mjs <download|upload|initialize|verify>'));

task.then((ok) => { if (!ok) process.exitCode = 1; }).catch((error) => {
  console.error(`[MEGA] ${error.message}`);
  process.exitCode = 1;
});
