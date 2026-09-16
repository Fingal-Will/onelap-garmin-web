import { authorizeGarmin as runGarminOAuth } from './oauth.js';

const API = 'https://api.github.com';
const WORKFLOWS = new Set(['sync.yml', 'reconcile.yml', 'garmin-session.yml']);
const OAUTH_WORKFLOW = 'garmin-session.yml';
const OAUTH_SECRET_PREFIX = 'GARMIN_OAUTH_';
const OAUTH_STALE_AFTER_MS = 30 * 60 * 1000;
const SECRET_NAMES = new Set([
  'ONELAP_ACCOUNT', 'ONELAP_PASSWORD', 'ONELAP_TOKEN', 'ONELAP_SESSION_KEY',
  'GARMIN_CN_OAUTH1', 'GARMIN_CN_OAUTH2', 'GARMIN_GLOBAL_OAUTH1', 'GARMIN_GLOBAL_OAUTH2',
  'GARMIN_DEVICE_UNIT_ID',
]);
const VARIABLE_NAMES = new Set([
  'GARMINIZE_FIT_ENABLED', 'FIT_COORDINATE_TRANSFORM_ENABLED', 'GARMIN_DEVICE_PRODUCT_ID',
  'GARMIN_DEVICE_SOFTWARE_VERSION', 'SYNC_TARGETS', 'SYNC_ENABLED',
]);
const MAX_SECRET_BYTES = 48 * 1024;
const MAX_VARIABLE_BYTES = 48 * 1024;
let sodiumPromise;

async function loadSodium() {
  sodiumPromise ??= import('libsodium-wrappers').then((module) => module.default ?? module);
  const sodium = await sodiumPromise;
  await sodium.ready;
  return sodium;
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

class GitHubError extends Error {
  constructor(status) {
    const reasons = {
      401: 'GitHub Token 无效或已过期，请重新连接。',
      403: 'GitHub 拒绝访问，请检查 Token 权限、组织授权或 API 用量限制。',
      404: '仓库或资源不存在，或当前 Token 没有访问权限。',
      409: '仓库状态发生冲突，请刷新后重试。',
      422: 'GitHub 未接受请求，请检查输入、工作流权限和分支保护设置。',
      429: 'GitHub API 请求过于频繁，请稍后重试。',
    };
    super(reasons[status] ?? `GitHub 请求未成功（HTTP ${Number(status) || 0}），请稍后重试。`);
    this.name = 'GitHubError';
    this.status = status;
  }
}

function decodeBase64(value) {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(
      Uint8Array.from(atob(value.replace(/\s/g, '')), (character) => character.charCodeAt(0)),
    );
  } catch {
    throw new Error('仓库配置文件无法读取，请检查安装是否完整。');
  }
}

function validManifest(value) {
  return isObject(value)
    && value.project === 'onelap-garmin-web'
    && value.schema_version === 1
    && typeof value.version === 'string'
    && value.version.trim().length > 0;
}

function safeComponent(value, type) {
  const string = String(value ?? '').trim();
  const pattern = type === 'owner' ? /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/ : /^[A-Za-z0-9_.-]{1,100}$/;
  if (!pattern.test(string) || string === '.' || string === '..') {
    throw new Error(type === 'owner' ? '请输入有效的 GitHub 用户名或组织名。' : '请输入有效的 GitHub 仓库名。');
  }
  return string;
}

function safeBranch(value) {
  const branch = String(value ?? '').trim();
  if (!branch || /[\x00-\x1f~^:?*\\[\\]\\]/.test(branch) || branch.startsWith('/')
    || branch.endsWith('/') || branch.includes('//') || branch.split('/').some((part) => part === '.' || part === '..')) {
    throw new Error('仓库默认分支名称无效，请在 GitHub 修复后重试。');
  }
  return branch;
}

function safeOAuthRequestId(value) {
  const requestId = String(value ?? '');
  if (!/^[A-F0-9]{32}$/.test(requestId)) {
    throw new Error('Garmin OAuth 请求编号无效。');
  }
  return requestId;
}

function safeOAuthRegion(value) {
  const region = String(value ?? '').toUpperCase();
  if (!['CN', 'GLOBAL'].includes(region)) throw new Error('Garmin OAuth 区域无效。');
  return region;
}

function oauthSecretNames(requestId) {
  const id = safeOAuthRequestId(requestId);
  return {
    username: `${OAUTH_SECRET_PREFIX}${id}_USERNAME`,
    password: `${OAUTH_SECRET_PREFIX}${id}_PASSWORD`,
    resultKey: `${OAUTH_SECRET_PREFIX}${id}_RESULT_KEY`,
  };
}

function oauthResultRef(requestId) {
  return `oauth-result-${safeOAuthRequestId(requestId)}`;
}

function isOAuthResultRef(value) {
  return /^refs\/heads\/oauth-result-[A-F0-9]{32}$/.test(value);
}

function isOAuthSecretName(name) {
  return /^GARMIN_OAUTH_[A-F0-9]{32}_(?:USERNAME|PASSWORD|RESULT_KEY)$/.test(name);
}

function oauthRequestIdFromSecret(name) {
  return /^GARMIN_OAUTH_([A-F0-9]{32})_(?:USERNAME|PASSWORD|RESULT_KEY)$/.exec(name)?.[1] ?? null;
}

function oauthRequestIdFromRef(ref) {
  return /^refs\/heads\/oauth-result-([A-F0-9]{32})$/.exec(ref)?.[1] ?? null;
}

function encodedSize(value) {
  return new TextEncoder().encode(value).length;
}

function validateVariable(name, value) {
  if (!VARIABLE_NAMES.has(name) || typeof value !== 'string'
    || new TextEncoder().encode(value).length > MAX_VARIABLE_BYTES) {
    throw new Error('设置包含不支持或过大的 Variable。');
  }
  const values = {
    GARMINIZE_FIT_ENABLED: new Set(['true', 'false']),
    FIT_COORDINATE_TRANSFORM_ENABLED: new Set(['true', 'false']),
    SYNC_ENABLED: new Set(['true', 'false']),
    SYNC_TARGETS: new Set(['CN', 'GLOBAL', 'CN,GLOBAL']),
  };
  if (values[name] && !values[name].has(value)) throw new Error(`${name} 的值无效。`);
  const asUint16 = /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) <= 65535;
  if (name === 'GARMIN_DEVICE_PRODUCT_ID' && (!asUint16 || Number(value) < 1)) {
    throw new Error('GARMIN_DEVICE_PRODUCT_ID 的值无效。');
  }
  if (name === 'GARMIN_DEVICE_SOFTWARE_VERSION' && value !== '' && !asUint16) {
    throw new Error('GARMIN_DEVICE_SOFTWARE_VERSION 的值无效。');
  }
}

function workflowInputValue(name, value) {
  if (typeof value === 'boolean') return String(value);
  if (typeof value !== 'string' || value.length > 128 || /[\x00-\x1f]/.test(value)) {
    throw new Error(`工作流参数 ${name} 无效。`);
  }
  return value;
}

function validateWorkflowInputs(workflow, inputs) {
  if (!isObject(inputs)) throw new Error('工作流参数格式无效。');
  const common = new Set(['target_regions', 'dry_run', 'request_id']);
  const allowed = workflow === 'sync.yml'
    ? new Set([...common, 'full_scan', 'retry_auth', 'record_id', 'force_reupload', 'discovery_pages'])
    : new Set([...common, 'recent_activities']);
  const normalized = {};
  for (const [name, value] of Object.entries(inputs)) {
    if (!allowed.has(name)) throw new Error('工作流参数无效；账号和凭据只能通过加密 Secrets 保存。');
    normalized[name] = workflowInputValue(name, value);
  }
  if (normalized.target_regions !== undefined && !['CN', 'GLOBAL', 'CN,GLOBAL'].includes(normalized.target_regions)) {
    throw new Error('target_regions 仅支持 CN、GLOBAL 或 CN,GLOBAL。');
  }
  for (const name of ['dry_run', 'full_scan', 'retry_auth', 'force_reupload']) {
    if (normalized[name] !== undefined && !['true', 'false'].includes(normalized[name])) {
      throw new Error(`${name} 只能为 true 或 false。`);
    }
  }
  if (workflow === 'sync.yml') {
    if (normalized.discovery_pages !== undefined && !['0', '1', '3', '10', '20', '50', '100'].includes(normalized.discovery_pages)) {
      throw new Error('discovery_pages 的值无效。');
    }
    if (normalized.record_id !== undefined && normalized.record_id !== '' && !/^[A-Za-z0-9_-]{1,128}$/.test(normalized.record_id)) {
      throw new Error('record_id 必须是有效的活动编号。');
    }
    if (normalized.force_reupload === 'true' && !normalized.record_id) {
      throw new Error('重新上传前必须填写活动编号。');
    }
  } else if (normalized.recent_activities !== undefined && !['1', '10', '20', '50', '100', '200'].includes(normalized.recent_activities)) {
    throw new Error('recent_activities 的值无效。');
  }
  if (normalized.request_id !== undefined && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(normalized.request_id)) {
    throw new Error('request_id 的格式无效。');
  }
  return normalized;
}

function validateBundle(bundle) {
  if (!bundle || typeof bundle.version !== 'string' || !Array.isArray(bundle.files)
    || !bundle.files.length || bundle.files.length > 500) {
    throw new Error('同步程序安装包格式无效，请重新构建控制台。');
  }
  const paths = new Set();
  let bytes = 0;
  for (const file of bundle.files) {
    if (!file || typeof file.path !== 'string' || !file.path || file.path.startsWith('/')
      || file.path.includes('\\') || /[\x00-\x1f]/.test(file.path)
      || file.path.split('/').some((part) => !part || part === '.' || part === '..' || part.toLowerCase() === '.git')
      || paths.has(file.path) || typeof file.content !== 'string'
      || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(file.content)) {
      throw new Error('同步程序安装包包含无效路径或内容。');
    }
    paths.add(file.path);
    bytes += file.content.length;
  }
  if (bytes > 16 * 1024 * 1024 || !paths.has('.onelap-web.json')
    || !paths.has('.github/workflows/sync.yml') || !paths.has('.github/workflows/reconcile.yml')
    || !paths.has('.github/workflows/garmin-session.yml')) {
    throw new Error('同步程序安装包不完整或体积过大。');
  }
  try {
    const marker = JSON.parse(decodeBase64(bundle.files.find((file) => file.path === '.onelap-web.json').content));
    if (!validManifest(marker) || marker.version !== bundle.version) throw new Error();
  } catch {
    throw new Error('同步程序安装包的版本标记无效。');
  }
}

export class GitHubClient {
  #token;
  #fetch;
  #controllers = new Set();
  #base;

  constructor({ token, owner, repo, fetchImpl = globalThis.fetch }) {
    this.owner = safeComponent(owner, 'owner');
    this.repo = safeComponent(repo, 'repo');
    if (typeof token !== 'string' || !token.trim() || /[\r\n]/.test(token)) {
      throw new Error('请输入有效的 GitHub Token。');
    }
    if (typeof fetchImpl !== 'function') throw new Error('当前环境不支持 GitHub 网络请求。');
    this.#token = token.trim();
    this.#fetch = fetchImpl;
    this.#base = `/repos/${encodeURIComponent(this.owner)}/${encodeURIComponent(this.repo)}`;
  }

  disconnect() {
    this.#token = '';
    for (const controller of this.#controllers) controller.abort();
    this.#controllers.clear();
  }

  async #request(path = '', { method = 'GET', body } = {}) {
    if (!this.#token) throw new Error('连接已断开，请重新输入 GitHub Token。');
    const url = new URL(`${API}${this.#base}${path}`);
    if (url.origin !== API || !url.pathname.startsWith(`${this.#base}/`) && url.pathname !== this.#base) {
      throw new Error('GitHub 请求地址无效。');
    }
    const controller = new AbortController();
    this.#controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), 30_000);
    let response;
    try {
      response = await this.#fetch(url.href, {
        method,
        headers: {
          Accept: 'application/vnd.github+json',
          Authorization: `Bearer ${this.#token}`,
          'X-GitHub-Api-Version': '2022-11-28',
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store',
        signal: controller.signal,
      });
    } catch {
      throw new Error(this.#token ? '无法连接 GitHub，或请求已超时。请检查网络后重试。' : '连接已断开，操作已停止。');
    } finally {
      clearTimeout(timer);
      this.#controllers.delete(controller);
    }
    if (!response.ok) throw new GitHubError(response.status);
    if (response.status === 204) return null;
    try {
      return await response.json();
    } catch {
      throw new Error('GitHub 返回了无法读取的数据，请稍后重试。');
    }
  }

  async #repository() {
    const repository = await this.#request();
    if (repository.private !== true) {
      throw new Error('只允许连接私有仓库。请先创建你自己的专用私有仓库。');
    }
    if (repository.archived || repository.disabled) throw new Error('仓库已归档或停用，请使用可写的私有仓库。');
    if (repository.permissions?.push === false) throw new Error('当前账号没有该私有仓库的写入权限。');
    if (typeof repository.default_branch !== 'string' || !repository.default_branch) {
      throw new Error('无法确认仓库默认分支，请在 GitHub 初始化仓库后重试。');
    }
    repository.default_branch = safeBranch(repository.default_branch);
    return repository;
  }

  async #paginated(path, key) {
    const items = [];
    const separator = path.includes('?') ? '&' : '?';
    for (let page = 1; page <= 20; page += 1) {
      const data = await this.#request(`${path}${separator}per_page=100&page=${page}`);
      if (!Array.isArray(data[key])) throw new Error('GitHub 返回的配置列表无效。');
      items.push(...data[key]);
      if (data[key].length < 100) return items;
    }
    throw new Error('仓库配置数量过多，请改用专用的同步私有仓库。');
  }

  async #manifest(branch) {
    let response;
    try {
      response = await this.#request(`/contents/.onelap-web.json?ref=${encodeURIComponent(branch)}`);
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
    try {
      const manifest = JSON.parse(decodeBase64(response.content));
      if (!validManifest(manifest)) throw new Error();
      return manifest;
    } catch {
      throw new Error('仓库中的同步安装标记无效，请检查仓库文件。');
    }
  }

  async #managedRepository() {
    const repository = await this.#repository();
    if (!await this.#manifest(repository.default_branch)) {
      throw new Error('请先安装同步程序，再保存设置或运行任务。');
    }
    return repository;
  }

  async inspectRepository() {
    const repository = await this.#repository();
    const results = await Promise.allSettled([
      this.#paginated('/actions/secrets', 'secrets'),
      this.#paginated('/actions/variables', 'variables'),
      this.#manifest(repository.default_branch),
    ]);
    for (const result of results) if (result.status === 'rejected') throw result.reason;
    const [secrets, variables, manifest] = results.map((result) => result.value);
    return {
      repository,
      secrets: secrets.map((entry) => entry.name),
      variables: Object.fromEntries(variables.map((entry) => [entry.name, entry.value])),
      installed: manifest !== null,
      manifest,
    };
  }

  /** Install only into a fresh dedicated repository, then publish one non-forced commit. */
  async installRunner(bundle, onProgress = () => {}) {
    validateBundle(bundle);
    if (typeof onProgress !== 'function') throw new Error('安装进度回调无效。');
    const repository = await this.#repository();
    const branch = repository.default_branch;
    const branchPath = branch.split('/').map(encodeURIComponent).join('/');
    const refPath = `/git/ref/heads/${branchPath}`;
    let ref;
    try {
      ref = await this.#request(refPath);
    } catch (error) {
      if (![404, 409].includes(error.status) || repository.size !== 0) throw error;
      onProgress('初始化空的私有仓库…');
      await this.#request('/contents/README.md', {
        method: 'PUT',
        body: {
          message: 'chore: initialize private sync repository',
          content: btoa('# OneLap Garmin Sync\n'),
          branch,
        },
      });
      ref = await this.#request(refPath);
    }
    if (!isObject(ref) || !isObject(ref.object) || typeof ref.object.sha !== 'string' || !/^[0-9a-f]{40,64}$/i.test(ref.object.sha)) {
      throw new Error('GitHub 返回的默认分支引用无效，已停止安装。');
    }
    const originalCommit = ref.object.sha;
    const commit = await this.#request(`/git/commits/${encodeURIComponent(originalCommit)}`);
    if (!isObject(commit) || !isObject(commit.tree) || typeof commit.tree.sha !== 'string' || !/^[0-9a-f]{40,64}$/i.test(commit.tree.sha)) {
      throw new Error('GitHub 返回的提交对象无效，已停止安装。');
    }
    const tree = await this.#request(`/git/trees/${encodeURIComponent(commit.tree.sha)}?recursive=1`);
    if (tree.truncated || !Array.isArray(tree.tree)) throw new Error('仓库文件列表不完整，已停止安装。');
    if (tree.tree.some((entry) => entry.path === '.onelap-web.json')) {
      throw new Error('此仓库已安装同步程序，无需重复安装。升级前请先备份和检查更改。');
    }
    const allowedInitialFile = /^(?:README(?:\.(?:md|txt|rst))?|LICENSE(?:\.(?:md|txt))?|\.gitignore)$/i;
    if (tree.tree.some((entry) => entry.type !== 'blob' || !['100644', '100755'].includes(entry.mode)
      || !allowedInitialFile.test(entry.path))) {
      throw new Error('此仓库包含其他文件。为避免覆盖，请使用空仓库或仅含 README、LICENSE、.gitignore 的新私有仓库。');
    }
    const existingPaths = new Set(tree.tree.map((entry) => entry.path));
    // Keep any allowed initialization files exactly as the repository owner created them.
    const files = bundle.files.filter((file) => !existingPaths.has(file.path));
    const entries = [];
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      onProgress(`正在安装同步程序（${index + 1}/${files.length}）…`);
      const blob = await this.#request('/git/blobs', {
        method: 'POST', body: { content: file.content, encoding: 'base64' },
      });
      entries.push({ path: file.path, mode: '100644', type: 'blob', sha: blob.sha });
    }
    const newTree = await this.#request('/git/trees', {
      method: 'POST', body: { base_tree: commit.tree.sha, tree: entries },
    });
    const newCommit = await this.#request('/git/commits', {
      method: 'POST',
      body: { message: 'feat: install OneLap Garmin private runner', tree: newTree.sha, parents: [originalCommit] },
    });
    const currentRef = await this.#request(refPath);
    if (currentRef.object.sha !== originalCommit) {
      throw new Error('安装期间仓库分支发生了变化，已停止发布；原有文件未被覆盖。请刷新后重试。');
    }
    await this.#repository();
    await this.#request(`/git/refs/heads/${branchPath}`, {
      method: 'PATCH', body: { sha: newCommit.sha, force: false },
    });
    onProgress('同步程序安装完成。');
    return `https://github.com/${encodeURIComponent(this.owner)}/${encodeURIComponent(this.repo)}/commit/${encodeURIComponent(newCommit.sha)}`;
  }

  async #saveEncryptedSecrets(secrets, isAllowedName, onProgress) {
    const pendingSecrets = Object.entries(secrets).filter(([, value]) => value.trim());
    if (!pendingSecrets.length) return [];
    const sodium = await loadSodium();
    const key = await this.#request('/actions/secrets/public-key');
    let publicKey;
    try {
      publicKey = { id: key.key_id, bytes: sodium.from_base64(key.key, sodium.base64_variants.ORIGINAL) };
      if (publicKey.bytes.length !== sodium.crypto_box_PUBLICKEYBYTES || typeof publicKey.id !== 'string') throw new Error();
    } catch {
      throw new Error('GitHub 返回的 Secret 加密公钥无效，未发送凭据。');
    }
    const saved = [];
    for (const [name, value] of pendingSecrets) {
      if (!isAllowedName(name) || typeof value !== 'string' || encodedSize(value) > MAX_SECRET_BYTES) {
        throw new Error('Secret 名称或内容无效。');
      }
      try {
        onProgress(`正在加密保存 ${name}…`);
        const encrypted = sodium.crypto_box_seal(sodium.from_string(value), publicKey.bytes);
        await this.#request(`/actions/secrets/${encodeURIComponent(name)}`, {
          method: 'PUT',
          body: { key_id: publicKey.id, encrypted_value: sodium.to_base64(encrypted, sodium.base64_variants.ORIGINAL) },
        });
        saved.push(name);
      } catch {
        throw new Error(`保存 ${name} 失败。已保存：${saved.length ? saved.join('、') : '无'}。请重新连接检查后重试；未完成项尚未保存。`);
      }
    }
    return saved;
  }

  async saveSettings(update = {}, onProgress = () => {}) {
    if (!isObject(update)) throw new Error('设置保存请求格式无效。');
    const { secrets = {}, variables = {} } = update;
    if (!isObject(secrets) || !isObject(variables) || typeof onProgress !== 'function') {
      throw new Error('设置保存请求格式无效。');
    }
    await this.#managedRepository();
    for (const [name, value] of Object.entries(secrets)) {
      if (!SECRET_NAMES.has(name) || typeof value !== 'string' || encodedSize(value) > MAX_SECRET_BYTES) {
        throw new Error('设置包含不支持或过大的 Secret。');
      }
    }
    for (const [name, value] of Object.entries(variables)) validateVariable(name, value);
    const saved = await this.#saveEncryptedSecrets(secrets, (name) => SECRET_NAMES.has(name), onProgress);
    const oldVariables = new Set((await this.#paginated('/actions/variables', 'variables')).map((entry) => entry.name));
    for (const [name, value] of Object.entries(variables)) {
      try {
        onProgress(`正在保存 ${name}…`);
        await this.#request(oldVariables.has(name) ? `/actions/variables/${name}` : '/actions/variables', {
          method: oldVariables.has(name) ? 'PATCH' : 'POST', body: { name, value },
        });
        saved.push(name);
      } catch {
        throw new Error(`保存 ${name} 失败。已保存：${saved.length ? saved.join('、') : '无'}。请重新连接检查后重试；未完成项尚未保存。`);
      }
    }
    return { saved };
  }

  async beginGarminOAuth({ requestId, region, username, password, resultKey }, onProgress = () => {}) {
    if (typeof onProgress !== 'function' || typeof username !== 'string' || !username.trim()
      || typeof password !== 'string' || !password || typeof resultKey !== 'string') {
      throw new Error('Garmin 账号、密码或结果密钥无效。');
    }
    const id = safeOAuthRequestId(requestId);
    const target = safeOAuthRegion(region);
    let resultKeyBytes;
    try {
      if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(resultKey)) throw new Error();
      resultKeyBytes = Uint8Array.from(atob(resultKey), (character) => character.charCodeAt(0));
    } catch {
      throw new Error('Garmin OAuth 结果密钥无效。');
    }
    if (resultKeyBytes.length !== 32 || encodedSize(username) > MAX_SECRET_BYTES
      || encodedSize(password) > MAX_SECRET_BYTES || encodedSize(resultKey) > MAX_SECRET_BYTES) {
      throw new Error('Garmin OAuth 临时凭据长度无效。');
    }
    const repository = await this.#managedRepository();
    const names = oauthSecretNames(id);
    await this.#saveEncryptedSecrets({
      [names.username]: username.trim(), [names.password]: password, [names.resultKey]: resultKey,
    }, (name) => isOAuthSecretName(name), onProgress);
    onProgress('正在提交 Garmin OAuth 获取任务…');
    await this.#request(`/actions/workflows/${OAUTH_WORKFLOW}/dispatches`, {
      method: 'POST', body: { ref: repository.default_branch, inputs: { region: target, request_id: id } },
    });
  }

  async getGarminOAuthRun(requestId) {
    const id = safeOAuthRequestId(requestId);
    await this.#repository();
    const data = await this.#request(`/actions/workflows/${OAUTH_WORKFLOW}/runs?event=workflow_dispatch&per_page=100`);
    if (!Array.isArray(data.workflow_runs)) throw new Error('GitHub 返回的 OAuth 运行记录无效。');
    const run = data.workflow_runs.find((entry) => entry?.display_title === id);
    if (!run) return null;
    if (!Number.isSafeInteger(run.id) || run.id < 1 || typeof run.status !== 'string') {
      throw new Error('GitHub 返回的 OAuth 运行记录无效。');
    }
    return {
      id: run.id,
      status: run.status,
      conclusion: typeof run.conclusion === 'string' ? run.conclusion : null,
      runAttempt: Number.isSafeInteger(run.run_attempt) ? run.run_attempt : null,
    };
  }

  async readGarminOAuthResult(requestId) {
    const id = safeOAuthRequestId(requestId);
    await this.#repository();
    const ref = oauthResultRef(id);
    let response;
    try {
      response = await this.#request(`/contents/result.json?ref=${encodeURIComponent(ref)}`);
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
    if (!isObject(response) || response.encoding !== 'base64' || typeof response.content !== 'string') {
      throw new Error('GitHub 返回的 OAuth 结果文件无效。');
    }
    const content = decodeBase64(response.content);
    if (encodedSize(content) > 256 * 1024) throw new Error('Garmin OAuth 结果文件过大。');
    return { ref, content };
  }

  async deleteGarminOAuthResult(requestId) {
    const id = safeOAuthRequestId(requestId);
    await this.#repository();
    try {
      await this.#request(`/git/refs/heads/${encodeURIComponent(oauthResultRef(id))}`, { method: 'DELETE' });
      return true;
    } catch (error) {
      if (error.status === 404) return false;
      throw error;
    }
  }

  async deleteGarminOAuthTemporarySecrets(requestId) {
    const names = Object.values(oauthSecretNames(requestId));
    await this.#repository();
    const failures = [];
    for (const name of names) {
      try {
        await this.#request(`/actions/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' });
      } catch (error) {
        if (error.status !== 404) failures.push(name);
      }
    }
    if (failures.length) throw new Error(`未能删除部分 Garmin OAuth 临时 Secret：${failures.join('、')}。`);
    return names;
  }

  async saveGarminOAuthSession(region, session, onProgress = () => {}) {
    const target = safeOAuthRegion(region);
    if (!isObject(session) || typeof session.oauth1 !== 'string' || !session.oauth1
      || typeof session.oauth2 !== 'string' || !session.oauth2 || typeof onProgress !== 'function') {
      throw new Error('Garmin OAuth 会话格式无效。');
    }
    await this.#managedRepository();
    return this.#saveEncryptedSecrets({
      [`GARMIN_${target}_OAUTH1`]: session.oauth1,
      [`GARMIN_${target}_OAUTH2`]: session.oauth2,
    }, (name) => SECRET_NAMES.has(name), onProgress);
  }

  async cleanupGarminOAuthSecrets({ exceptRequestId } = {}) {
    const except = exceptRequestId === undefined ? null : safeOAuthRequestId(exceptRequestId);
    await this.#repository();
    const [secretEntries, workflowRuns, refs] = await Promise.all([
      this.#paginated('/actions/secrets', 'secrets'),
      this.#paginated(`/actions/workflows/${OAUTH_WORKFLOW}/runs?event=workflow_dispatch`, 'workflow_runs'),
      this.#request('/git/matching-refs/heads/oauth-result-').then((entries) => {
        if (!Array.isArray(entries)) throw new Error('无法读取残留 Garmin OAuth 结果分支。');
        return entries;
      }).catch(() => { throw new Error('无法读取残留 Garmin OAuth 结果分支。'); }),
    ]);
    const activeRequestIds = new Set();
    const completedRequestIds = new Set();
    for (const run of workflowRuns) {
      const requestId = typeof run?.display_title === 'string' && /^[A-F0-9]{32}$/.test(run.display_title)
        ? run.display_title : null;
      if (!requestId) continue;
      if (run.status === 'completed') completedRequestIds.add(requestId);
      else activeRequestIds.add(requestId);
    }

    const secretsByRequest = new Map();
    for (const entry of secretEntries) {
      const name = entry?.name;
      if (typeof name !== 'string' || !isOAuthSecretName(name)) continue;
      const requestId = oauthRequestIdFromSecret(name);
      if (!secretsByRequest.has(requestId)) secretsByRequest.set(requestId, []);
      secretsByRequest.get(requestId).push(entry);
    }
    const now = Date.now();
    const protectedRequestIds = new Set(activeRequestIds);
    const names = [];
    for (const [requestId, entries] of secretsByRequest) {
      const timestamps = entries.map((entry) => Date.parse(entry.updated_at)).filter(Number.isFinite);
      const recentWithoutRun = !completedRequestIds.has(requestId)
        && (timestamps.length !== entries.length || timestamps.some((timestamp) => now - timestamp < OAUTH_STALE_AFTER_MS));
      if (requestId === except || activeRequestIds.has(requestId) || recentWithoutRun) {
        protectedRequestIds.add(requestId);
        continue;
      }
      names.push(...entries.map((entry) => entry.name));
    }
    const failures = [];
    for (const name of names) {
      try {
        await this.#request(`/actions/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' });
      } catch (error) {
        if (error.status !== 404) failures.push(name);
      }
    }
    const resultRefs = refs.map((entry) => entry?.ref)
      .filter((ref) => typeof ref === 'string' && isOAuthResultRef(ref)
        && (!except || ref !== `refs/heads/${oauthResultRef(except)}`)
        && !protectedRequestIds.has(oauthRequestIdFromRef(ref)));
    for (const ref of resultRefs) {
      const shortRef = ref.slice('refs/heads/'.length);
      try {
        await this.#request(`/git/refs/heads/${encodeURIComponent(shortRef)}`, { method: 'DELETE' });
      } catch (error) {
        if (error.status !== 404) failures.push(ref);
      }
    }
    if (failures.length) throw new Error(`未能清除部分残留 Garmin OAuth 资源：${failures.join('、')}。`);
    return { secrets: names, refs: resultRefs, protectedRequestIds: [...protectedRequestIds].sort() };
  }

  async authorizeGarmin(options = {}) {
    return runGarminOAuth({ ...options, client: this });
  }

  async dispatchWorkflow(workflow, inputs = {}) {
    if (!WORKFLOWS.has(workflow) || workflow === OAUTH_WORKFLOW) throw new Error('请使用 Garmin OAuth 专用工作流接口。');
    const repository = await this.#managedRepository();
    const normalizedInputs = validateWorkflowInputs(workflow, inputs);
    await this.#request(`/actions/workflows/${workflow}/dispatches`, {
      method: 'POST', body: { ref: repository.default_branch, inputs: normalizedInputs },
    });
  }

  async listRuns() {
    await this.#repository();
    const results = await Promise.allSettled([...WORKFLOWS].map(async (workflow) => {
      try {
        const data = await this.#request(`/actions/workflows/${workflow}/runs?per_page=10`);
        return data.workflow_runs ?? [];
      } catch (error) {
        if (error.status === 404) return [];
        throw error;
      }
    }));
    for (const result of results) if (result.status === 'rejected') throw result.reason;
    return results.flatMap((result) => result.value)
      .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)))
      .slice(0, 20);
  }

  async setScheduleEnabled(enabled) {
    if (typeof enabled !== 'boolean') throw new Error('自动同步开关值无效。');
    return this.saveSettings({ variables: { SYNC_ENABLED: String(enabled) } });
  }
}
