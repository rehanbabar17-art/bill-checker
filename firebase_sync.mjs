import { Storage } from 'megajs';
import 'dotenv/config';
import fs from 'node:fs';
import path from 'node:path';
import { cert, getApps, initializeApp } from 'firebase-admin/app';
import { getDatabase } from 'firebase-admin/database';

const ROOT_DIR = process.cwd();
const CONFIG_FILE = path.join(ROOT_DIR, 'config.json');
const STATE_FILE = path.join(ROOT_DIR, 'bill_state.json');
const FIREBASE_ROOT = 'bill-checker';
const MEGA_EMAIL = process.env.MEGA_EMAIL;
const MEGA_PASSWORD = process.env.MEGA_PASSWORD;
const FIREBASE_TIMEOUT_MS = 20_000;

class SyncFailure extends Error {}

function withTimeout(promise, operation) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new SyncFailure(`Firebase ${operation} timed out after ${FIREBASE_TIMEOUT_MS / 1000} seconds.`)), FIREBASE_TIMEOUT_MS);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function safeFailureCategory(error) {
  const code = typeof error?.code === 'string' ? error.code : '';
  const message = error instanceof Error ? error.message : String(error ?? '');
  if (code) return `External service returned safe error code ${code}.`;
  if (message.includes('FIREBASE_SERVICE_ACCOUNT')) return 'Firebase service-account secret is missing, invalid JSON, or missing required fields.';
  if (code === 'PERMISSION_DENIED' || /permission[_ ]denied|PERMISSION_DENIED/i.test(message)) return 'Firebase database permission was denied; check Realtime Database rules and service-account access.';
  if (code === 'INVALID_ARGUMENT' || /invalid argument|invalid database/i.test(message)) return 'Firebase rejected the database URL or request configuration.';
  if (/private key|PEM|DECODER|invalid_grant|unauthorized_client/i.test(message)) return 'Firebase rejected the service-account private key or authentication.';
  if (/ENOTFOUND|ECONN|ETIMEDOUT|timed out|network/i.test(message)) return 'Firebase or Mega network request failed.';
  if (/MEGA|EKEY|EPASSWORD|EBLOCKED|credentials/i.test(message)) return 'Mega authentication or storage access failed.';
  return 'Firebase or Mega authentication, database, or network error.';
}

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
  if (value.iesco === undefined && value.sngpl === undefined) {
    throw new SyncFailure('config.json must contain an iesco or sngpl array.');
  }
  if (value.ntfy_key !== undefined && typeof value.ntfy_key !== 'string') {
    throw new SyncFailure('config.json field ntfy_key must be a string.');
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

function referenceGuide(config) {
  const lines = [
    'UTILITY BILL REFERENCE NUMBERS',
    '================================',
    '',
    'IESCO REFERENCE NUMBERS',
    '-----------------------',
    ...((config.iesco ?? []).map((account) => `${account.name ?? 'IESCO'}: ${account.ref ?? account.consumer ?? '(missing reference)'}`)),
    ...((config.iesco ?? []).length ? [] : ['(none configured)']),
    '',
    'SNGPL REFERENCE NUMBERS',
    '-----------------------',
    ...((config.sngpl ?? []).map((account) => `${account.name ?? 'SNGPL'}: ${account.consumer ?? account.ref ?? '(missing reference)'}`)),
    ...((config.sngpl ?? []).length ? [] : ['(none configured)']),
    '',
    'This private guide is generated from config.json.',
    '',
  ];
  return lines.join('\n');
}

function firebaseDatabase() {
  const rawServiceAccount = process.env.FIREBASE_SERVICE_ACCOUNT;
  const databaseURL = process.env.FIREBASE_DATABASE_URL?.replace(/\/$/, '');
  if (!rawServiceAccount || !databaseURL) {
    throw new SyncFailure('FIREBASE_SERVICE_ACCOUNT and FIREBASE_DATABASE_URL Actions secrets are required.');
  }
  const serviceAccount = parseJson(rawServiceAccount, 'FIREBASE_SERVICE_ACCOUNT');
  if (!serviceAccount.client_email || !serviceAccount.private_key || !serviceAccount.project_id) {
    throw new SyncFailure('FIREBASE_SERVICE_ACCOUNT is missing required fields.');
  }
  const app = getApps()[0] ?? initializeApp({ credential: cert(serviceAccount), databaseURL });
  return getDatabase(app);
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

async function openMega() {
  if (!MEGA_EMAIL || !MEGA_PASSWORD) throw new SyncFailure('MEGA_EMAIL and MEGA_PASSWORD Actions secrets are required for migration.');
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    let storage;
    try {
      storage = await new Storage({ email: MEGA_EMAIL, password: MEGA_PASSWORD }).ready;
      await storage.reload(true);
      const rootData = findFolder(storage.root, 'github-data');
      const billFolder = rootData ? findFolder(rootData, 'bill-checker') : undefined;
      if (!billFolder) {
        await storage.close().catch(() => undefined);
        throw new SyncFailure('MEGA folder github-data/bill-checker was not found.');
      }
      return { storage, folder: billFolder };
    } catch (error) {
      lastError = error;
      await storage?.close().catch(() => undefined);
      if (error instanceof SyncFailure) throw error;
      if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, 1500 * attempt));
    }
  }
  throw new SyncFailure('MEGA login or storage access failed after 3 attempts.');
}

async function megaFile(folder, name) {
  const node = newestFile(folder, name);
  return node ? node.downloadBuffer({}) : undefined;
}

async function readFirebase() {
  const db = firebaseDatabase();
  const snapshot = await withTimeout(db.ref(FIREBASE_ROOT).once('value'), 'read');
  const value = snapshot.val();
  if (!isRecord(value)) throw new SyncFailure('Firebase bill-checker data is missing or has an invalid shape.');
  const config = value.config;
  const state = value.state;
  const guide = value['reference-guide'];
  if (!isRecord(config)) throw new SyncFailure('Firebase bill-checker/config is missing or invalid.');
  if (!isRecord(state)) throw new SyncFailure('Firebase bill-checker/state is missing or invalid.');
  parseConfig(JSON.stringify(config));
  parseState(JSON.stringify(state));
  return { config, state, guide: typeof guide === 'string' ? guide : referenceGuide(config) };
}

async function downloadFromFirebase() {
  try {
    const data = await readFirebase();
    writePrivateFile(CONFIG_FILE, Buffer.from(JSON.stringify(data.config, null, 2) + '\n', 'utf8'));
    writePrivateFile(STATE_FILE, Buffer.from(JSON.stringify(data.state, null, 2) + '\n', 'utf8'));
    console.log('[FIREBASE] Restored config.json and bill_state.json from Firebase.');
    return true;
  } catch (error) {
    console.error(`[FIREBASE] Download failed: ${error instanceof SyncFailure ? error.message : safeFailureCategory(error)}`);
    return false;
  }
}

async function uploadToFirebase() {
  try {
    if (!fs.existsSync(CONFIG_FILE) || !fs.existsSync(STATE_FILE)) throw new SyncFailure('Local config.json and bill_state.json are required.');
    const config = parseConfig(fs.readFileSync(CONFIG_FILE));
    const state = parseState(fs.readFileSync(STATE_FILE));
    const db = firebaseDatabase();
    await withTimeout(db.ref(FIREBASE_ROOT).set({ config, state, 'reference-guide': referenceGuide(config) }), 'write');
    console.log('[FIREBASE] Uploaded validated config, state, and labeled reference guide.');
    return true;
  } catch (error) {
    console.error(`[FIREBASE] Upload failed: ${error instanceof SyncFailure ? error.message : safeFailureCategory(error)}`);
    return false;
  }
}

async function migrateFromMega() {
  let storage;
  try {
    console.log('[FIREBASE] Reading config and reference guide from Mega; bill history will be initialized/preserved in Firebase.');
    const mega = await openMega();
    storage = mega.storage;
    const configData = await megaFile(mega.folder, 'config.json');
    const guideData = await megaFile(mega.folder, 'REFERENCE_NUMBERS.txt');
    if (!configData) throw new SyncFailure('MEGA config.json is missing.');
    const config = parseConfig(configData);
    const guide = guideData?.toString('utf8') || referenceGuide(config);
    console.log(`[FIREBASE] Mega read succeeded: ${(config.iesco ?? []).length} IESCO and ${(config.sngpl ?? []).length} SNGPL accounts.`);
    console.log('[FIREBASE] Connecting to Firebase and writing the staged copy.');
    const db = firebaseDatabase();
    const existing = await withTimeout(db.ref(FIREBASE_ROOT).once('value'), 'read');
    const existingValue = existing.val();
    const state = isRecord(existingValue?.state) ? existingValue.state : {};
    await withTimeout(db.ref(FIREBASE_ROOT).update({ config, state, 'reference-guide': guide }), 'write');
    const verified = await readFirebase();
    console.log(`[FIREBASE] Migration complete and verified: ${(verified.config.iesco ?? []).length} IESCO, ${(verified.config.sngpl ?? []).length} SNGPL accounts, ${Object.keys(verified.state).length} Firebase bill-state records.`);
    return true;
  } catch (error) {
    console.error(`[FIREBASE] Migration failed: ${error instanceof SyncFailure ? error.message : safeFailureCategory(error)}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

async function verifyFirebase() {
  try {
    const data = await readFirebase();
    console.log(`[FIREBASE-VERIFY] Authentication succeeded; ${(data.config.iesco ?? []).length} IESCO and ${(data.config.sngpl ?? []).length} SNGPL accounts are present.`);
    console.log(`[FIREBASE-VERIFY] ${Object.keys(data.state).length} bill-state records are present.`);
    console.log(`[FIREBASE-VERIFY] Labeled reference guide is present (${data.guide.length} bytes).`);
    return true;
  } catch (error) {
    console.error(`[FIREBASE-VERIFY] ${error instanceof SyncFailure ? error.message : safeFailureCategory(error)}`);
    return false;
  }
}

async function initializeFirebaseState() {
  try {
    const db = firebaseDatabase();
    const existing = await withTimeout(db.ref(FIREBASE_ROOT).once('value'), 'read');
    const value = existing.val();
    if (!isRecord(value?.config)) throw new SyncFailure('Firebase bill-checker/config must exist before initializing state.');
    if (isRecord(value.state)) {
      parseState(JSON.stringify(value.state));
      console.log(`[FIREBASE] Existing bill history preserved (${Object.keys(value.state).length} records).`);
      return true;
    }
    await withTimeout(db.ref(`${FIREBASE_ROOT}/state`).set({}), 'write');
    await withTimeout(db.ref(`${FIREBASE_ROOT}/reference-guide`).set(typeof value['reference-guide'] === 'string'
      ? value['reference-guide']
      : referenceGuide(value.config)), 'write');
    console.log('[FIREBASE] Initialized bill-checker/state directly in Firebase as an empty history object.');
    return true;
  } catch (error) {
    console.error(`[FIREBASE] State initialization failed: ${error instanceof SyncFailure ? error.message : safeFailureCategory(error)}`);
    return false;
  }
}

const command = process.argv[2];
const task = command === 'download' ? downloadFromFirebase()
  : command === 'upload' ? uploadToFirebase()
    : command === 'migrate-from-mega' ? migrateFromMega()
      : command === 'initialize-state' ? initializeFirebaseState()
        : command === 'verify' ? verifyFirebase()
          : Promise.reject(new Error('Usage: node firebase_sync.mjs <download|upload|migrate-from-mega|initialize-state|verify>'));

task.then((ok) => { if (!ok) process.exitCode = 1; }).catch((error) => {
  console.error(`[FIREBASE] ${error.message}`);
  process.exitCode = 1;
});
