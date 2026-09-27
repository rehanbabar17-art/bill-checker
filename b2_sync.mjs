import { Storage } from 'megajs';
import 'dotenv/config';
import fs from 'node:fs';
import path from 'node:path';
import { GetObjectCommand, HeadObjectCommand, PutObjectCommand, S3Client } from '@aws-sdk/client-s3';

const ROOT_DIR = process.cwd();
const CONFIG_FILE = path.join(ROOT_DIR, 'config.json');
const STATE_FILE = path.join(ROOT_DIR, 'bill_state.json');
const CONFIG_KEY = 'config.json';
const STATE_KEY = 'bill_state.json';
const GUIDE_KEY = 'REFERENCE_NUMBERS.txt';
const B2_TIMEOUT_MS = 20_000;
const MEGA_EMAIL = process.env.MEGA_EMAIL;
const MEGA_PASSWORD = process.env.MEGA_PASSWORD;

class SyncFailure extends Error {}

function withTimeout(promise, operation) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new SyncFailure(`B2 ${operation} timed out after ${B2_TIMEOUT_MS / 1000} seconds.`)), B2_TIMEOUT_MS);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
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
    if (value[key] !== undefined && !Array.isArray(value[key])) throw new SyncFailure(`config.json field ${key} must be an array.`);
  }
  if (value.iesco === undefined && value.sngpl === undefined) throw new SyncFailure('config.json must contain an iesco or sngpl array.');
  return value;
}

function parseState(input) {
  const value = parseJson(input, 'bill_state.json');
  if (!isRecord(value)) throw new SyncFailure('bill_state.json must be a JSON object.');
  return value;
}

function referenceGuide(config) {
  const lines = ['UTILITY BILL REFERENCE NUMBERS', '================================', '', 'IESCO REFERENCE NUMBERS', '-----------------------'];
  for (const account of config.iesco ?? []) lines.push(`${account.name ?? 'IESCO'}: ${account.ref ?? account.consumer ?? '(missing reference)'}`);
  if (!(config.iesco ?? []).length) lines.push('(none configured)');
  lines.push('', 'SNGPL REFERENCE NUMBERS', '-----------------------');
  for (const account of config.sngpl ?? []) lines.push(`${account.name ?? 'SNGPL'}: ${account.consumer ?? account.ref ?? '(missing reference)'}`);
  if (!(config.sngpl ?? []).length) lines.push('(none configured)');
  lines.push('', 'This private guide is generated from config.json.', '');
  return `${lines.join('\n')}\n`;
}

function b2Client() {
  const region = 'us-east-005';
  const endpoint = (process.env.B2_ENDPOINT || 'https://s3.us-east-005.backblazeb2.com').replace(/\/$/, '');
  const bucket = process.env.B2_BUCKET;
  if (!process.env.B2_KEY_ID || !process.env.B2_APPLICATION_KEY || !bucket) {
    throw new SyncFailure('B2_KEY_ID, B2_APPLICATION_KEY, and B2_BUCKET Actions secrets are required.');
  }
  return { bucket, client: new S3Client({ region, endpoint, forcePathStyle: true, credentials: { accessKeyId: process.env.B2_KEY_ID, secretAccessKey: process.env.B2_APPLICATION_KEY } }) };
}

async function bodyBuffer(body) {
  if (!body) return undefined;
  if (typeof body.transformToByteArray === 'function') return Buffer.from(await body.transformToByteArray());
  const chunks = [];
  for await (const chunk of body) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

async function b2Get(client, bucket, key, required = false) {
  try {
    const result = await withTimeout(client.send(new GetObjectCommand({ Bucket: bucket, Key: key })), `read ${key}`);
    return bodyBuffer(result.Body);
  } catch (error) {
    const status = error?.$metadata?.httpStatusCode;
    if (status === 404 || error?.name === 'NoSuchKey' || error?.name === 'NotFound') {
      if (!required) return undefined;
      throw new SyncFailure(`B2 object ${key} is missing from bucket ${bucket}.`);
    }
    if (status === 401 || status === 403 || /AccessDenied|InvalidAccessKeyId|SignatureDoesNotMatch/i.test(error?.name ?? '')) {
      throw new SyncFailure(`B2 access was denied while reading ${key}; check the key ID, application key, bucket permission, and endpoint.`);
    }
    if (error instanceof SyncFailure) throw error;
    throw new SyncFailure(`Could not read B2 object ${key}.`);
  }
}

async function b2Put(client, bucket, key, body) {
  try {
    await withTimeout(client.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: key.endsWith('.json') ? 'application/json' : 'text/plain' })), `write ${key}`);
  } catch (error) {
    if (error instanceof SyncFailure) throw error;
    throw new SyncFailure(`Could not write B2 object ${key}.`);
  }
}

async function readB2() {
  const { bucket, client } = b2Client();
  const configData = await b2Get(client, bucket, CONFIG_KEY, true);
  const stateData = await b2Get(client, bucket, STATE_KEY, true);
  const guideData = await b2Get(client, bucket, GUIDE_KEY);
  const config = parseConfig(configData);
  const state = parseState(stateData);
  return { config, state, guide: guideData?.toString('utf8') || referenceGuide(config) };
}

async function downloadFromB2() {
  try {
    const data = await readB2();
    fs.writeFileSync(CONFIG_FILE, `${JSON.stringify(data.config, null, 2)}\n`, { mode: 0o600 });
    fs.writeFileSync(STATE_FILE, `${JSON.stringify(data.state, null, 2)}\n`, { mode: 0o600 });
    console.log(`[B2] Restored config and state (${Object.keys(data.state).length} bill-state records).`);
    return true;
  } catch (error) {
    console.error(`[B2] Download failed: ${error instanceof SyncFailure ? error.message : 'B2 authentication, storage, or network error.'}`);
    return false;
  }
}

async function uploadToB2() {
  try {
    if (!fs.existsSync(CONFIG_FILE) || !fs.existsSync(STATE_FILE)) throw new SyncFailure('Local config.json and bill_state.json are required.');
    const configData = fs.readFileSync(CONFIG_FILE);
    const stateData = fs.readFileSync(STATE_FILE);
    const config = parseConfig(configData);
    parseState(stateData);
    const { bucket, client } = b2Client();
    await b2Put(client, bucket, CONFIG_KEY, configData);
    await b2Put(client, bucket, STATE_KEY, stateData);
    await b2Put(client, bucket, GUIDE_KEY, Buffer.from(referenceGuide(config), 'utf8'));
    console.log('[B2] Uploaded validated config, bill state, and labeled reference guide.');
    return true;
  } catch (error) {
    console.error(`[B2] Upload failed: ${error instanceof SyncFailure ? error.message : 'B2 authentication, storage, or network error.'}`);
    return false;
  }
}

function newestFile(folder, name) {
  return (folder.children ?? []).filter((node) => !node.directory && node.name === name)
    .reduce((newest, node) => (!newest || Number(node.timestamp ?? 0) >= Number(newest.timestamp ?? 0) ? node : newest), undefined);
}

function findFolder(folder, name) {
  return (folder.children ?? []).find((node) => node.directory && node.name === name);
}

async function openMega() {
  if (!MEGA_EMAIL || !MEGA_PASSWORD) throw new SyncFailure('MEGA_EMAIL and MEGA_PASSWORD Actions secrets are required for migration.');
  let storage;
  try {
    storage = await withTimeout(new Storage({ email: MEGA_EMAIL, password: MEGA_PASSWORD }).ready, 'Mega login');
    await withTimeout(storage.reload(true), 'Mega listing');
    const rootData = findFolder(storage.root, 'github-data');
    const folder = rootData ? findFolder(rootData, 'bill-checker') : undefined;
    if (!folder) throw new SyncFailure('MEGA folder github-data/bill-checker was not found.');
    return { storage, folder };
  } catch (error) {
    await storage?.close().catch(() => undefined);
    if (error instanceof SyncFailure) throw error;
    throw new SyncFailure('Mega login or storage access failed.');
  }
}

async function migrateFromMega() {
  let storage;
  try {
    console.log('[B2] Reading config and reference guide from Mega; bill history will be initialized/preserved in B2.');
    const mega = await openMega();
    storage = mega.storage;
    const configNode = newestFile(mega.folder, 'config.json');
    const guideNode = newestFile(mega.folder, GUIDE_KEY);
    if (!configNode) throw new SyncFailure('MEGA config.json is missing.');
    const configData = await withTimeout(configNode.downloadBuffer({}), 'Mega config read');
    const guideData = guideNode ? await withTimeout(guideNode.downloadBuffer({}), 'Mega guide read') : undefined;
    const config = parseConfig(configData);
    const { bucket, client } = b2Client();
    const existingState = await b2Get(client, bucket, STATE_KEY);
    const state = existingState ? parseState(existingState) : {};
    await b2Put(client, bucket, CONFIG_KEY, configData);
    await b2Put(client, bucket, STATE_KEY, Buffer.from(`${JSON.stringify(state, null, 2)}\n`, 'utf8'));
    await b2Put(client, bucket, GUIDE_KEY, guideData || Buffer.from(referenceGuide(config), 'utf8'));
    const verified = await readB2();
    console.log(`[B2] Migration complete and verified: ${(verified.config.iesco ?? []).length} IESCO, ${(verified.config.sngpl ?? []).length} SNGPL accounts, ${Object.keys(verified.state).length} B2 bill-state records.`);
    return true;
  } catch (error) {
    console.error(`[B2] Migration failed: ${error instanceof SyncFailure ? error.message : 'B2 or Mega authentication, storage, or network error.'}`);
    return false;
  } finally {
    await storage?.close().catch(() => undefined);
  }
}

async function verifyB2() {
  try {
    const data = await readB2();
    console.log(`[B2-VERIFY] Authentication succeeded; ${(data.config.iesco ?? []).length} IESCO and ${(data.config.sngpl ?? []).length} SNGPL accounts present.`);
    console.log(`[B2-VERIFY] ${Object.keys(data.state).length} bill-state records present.`);
    console.log(`[B2-VERIFY] Labeled reference guide present (${data.guide.length} bytes).`);
    return true;
  } catch (error) {
    console.error(`[B2-VERIFY] Verification failed: ${error instanceof SyncFailure ? error.message : 'B2 authentication, storage, or network error.'}`);
    return false;
  }
}

const command = process.argv[2];
const task = command === 'download' ? downloadFromB2()
  : command === 'upload' ? uploadToB2()
    : command === 'migrate-from-mega' ? migrateFromMega()
      : command === 'verify' ? verifyB2()
        : Promise.reject(new Error('Usage: node b2_sync.mjs <download|upload|migrate-from-mega|verify>'));

task.then((ok) => { if (!ok) process.exitCode = 1; }).catch((error) => {
  console.error(`[B2] ${error.message}`);
  process.exitCode = 1;
});
