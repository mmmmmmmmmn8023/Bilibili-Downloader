/**
 * app.js - 前端交互逻辑
 * =====================
 * 负责：搜索UP主、展示视频/动态列表、触发下载、轮询进度
 */

// 全局状态
let currentData = null;   // 当前搜索结果
let currentTab = "dynamics"; // 当前选中的标签（视频标签已移除，默认「全部」）
let cookie = "";           // SESSDATA cookie
let downloadTasks = {};    // 下载任务进度
let prevActiveCount = 0;   // 上一轮"进行中"任务数（用于完成提示判定）
let pollTimer = null;      // 轮询定时器
let downloadedItems = new Set();  // 已下载记录：{data-dyid}|{bvid} 键集合

function markDownloaded(dataDyid, bvid) {
  if (dataDyid && dataDyid !== "0") downloadedItems.add("dy:" + dataDyid);
  if (bvid && bvid !== "0") downloadedItems.add("bv:" + bvid);
}
function unmarkDownloaded(dataDyid, bvid) {
  if (dataDyid && dataDyid !== "0") downloadedItems.delete("dy:" + dataDyid);
  if (bvid && bvid !== "0") downloadedItems.delete("bv:" + bvid);
}
function isDownloadedCheck(dataDyid, bvid) {
  if (dataDyid && dataDyid !== "0" && downloadedItems.has("dy:" + dataDyid)) return true;
  if (bvid && bvid !== "0" && downloadedItems.has("bv:" + bvid)) return true;
  return false;
}

// 分页状态
let currentUid = "";        // 当前搜索的UID
let currentVideoPage = 1;   // 当前视频页码
let videoPerPage = 12;      // 每页视频数（与「我的」页面一致，4 列 × 3 行）
let videoTotal = 0;         // 视频总数

// 动态分页状态（每页10条，点下一页才向后拉一批，避免一次性狂拉触发B站风控）
let currentDynPage = 1;     // 当前动态页码
let dynPerPage = 12;        // 每页动态数（与B站 feed/space 原生批次对齐）
let dynHasMore = false;     // 缓冲里是否还有更多
let dynLoaded = 0;          // 缓冲累计已加载条数
let dynImageText = 0;       // 缓冲里图文(type==image)条数（简介里展示）
let dynTypeFilter = "全部"; // 动态类型筛选（全部/投稿视频/动态视频/充电专属/图文/文字/转发…），按 dy-type 分类
let dynTypeCounts = {};     // 整个已加载缓冲的各类型计数（筛选栏用，服务端返回）
let dynFilteredLoaded = 0;  // 当前筛选类型在缓冲里的总条数（筛选分页用）


// 可选批量下载
let selectedVideos = new Set();   // 勾选的视频bvid
let selectedDynamics = new Set(); // 勾选的动态id

// 下载任务→项目映射（用于检测下载成功后标记"已下载"）
let taskItemMap = {};  // { task_id: { type: "video"|"dynamic", id: "bvid"|dynamic_id } }

// ==================== 初始化（页面加载时自动读取保存的SESSDATA）====================

async function initApp() {
  // 1. 先从localStorage读取
  const saved = localStorage.getItem("bilibili_sessdata");
  if (saved) {
    cookie = saved;
    updateCookieStatus();
  }
  // 2. 再从服务器配置文件读取（如果localStorage没有）
  if (!cookie) {
    try {
      const resp = await fetch("/api/config");
      const data = await resp.json();
      if (data.has_sessdata) {
        cookie = data.sessdata;
        localStorage.setItem("bilibili_sessdata", cookie);
        updateCookieStatus();
      }
    } catch (e) {
      // 忽略
    }
  }
  // 3. 加载下载历史
  try {
    const resp = await fetch("/api/history");
    const data = await resp.json();
    (Array.isArray(data) ? data : []).forEach(item => {
      if (item["data-dyid"] && item["data-dyid"] !== "0") downloadedItems.add("dy:" + item["data-dyid"]);
      if (item.bvid && item.bvid !== "0") downloadedItems.add("bv:" + item.bvid);
    });
  } catch (e) {
    // 忽略
  }
  // 4. 验证 SESSDATA 是否有效（让用户一打开就知道状态，后端会从配置兜底读取）
  verifyCookie();
  // 5. 填充 navbar 上的登录用户头像（『我的』入口）
  loadSelfChip();
  // 6. 加载常用UP主列表（localStorage 持久化）
  loadFavUps();
  // 7. 每 5 分钟自动复检一次，SESSDATA 过期时能实时反映到状态灯
  renderDownloadPanel(); // 初始化下载面板（空闲态统计）
  renderDownloadTypeCheckboxes(); // 预渲染下载类型复选框（与 TAB 栏同步，默认全选；打开设置时再用服务端配置覆盖）
  if (!window.__cookieWatchTimer) {
    window.__cookieWatchTimer = setInterval(verifyCookie, 5 * 60 * 1000);
  }
  // 8. 常驻轮询自动化下载实时日志（即使弹窗关闭也更新右下角状态）
  startAutoLogPolling();
  // 9. 页面一加载就常驻轮询下载任务面板。
  //    否则硬刷(Ctrl+F5)后若没手动点过下载，轮询不会启动，
  //    此时「立即检查」自动下发的任务虽已写入服务端 download_tasks，
  //    但前端镜像为空 → 面板显示「暂无下载任务」。
  startPolling();
  // 10. 首次打开且无 cookie → 弹出设置弹窗
  if (!cookie) openCookieModal();
}

// ==================== 搜索 ====================

let _searchTimer = null;  // 0.5s 防抖，避免快速连击搜索

async function doSearch() {
  const input = document.getElementById("searchInput");
  const query = input.value.trim();
  if (!query) {
    alert("请输入UP主UID或主页链接");
    return;
  }
  if (_searchTimer) clearTimeout(_searchTimer);
  const btn = document.getElementById("searchBtn");
  btn.disabled = true;
  btn.textContent = "...";
  _searchTimer = setTimeout(() => {
    _searchTimer = null;
    searchByQuery(query);
  }, 500);
}

// 核心搜索逻辑（输入框与「常用UP主」列表共用）
async function searchByQuery(query) {
  const input = document.getElementById("searchInput");
  if (input) input.value = query;  // 回显当前查询（点常用UP主时输入框显示其UID）
  const btn = document.getElementById("searchBtn");
  btn.disabled = true;
  btn.textContent = "搜索中...";
  document.getElementById("userCard").style.display = "none";
  document.getElementById("tabs").style.display = "none";
  document.getElementById("content").innerHTML = `
    <div class="loading">
      <div class="spinner"></div>
      正在搜索，请稍候...
    </div>
  `;

  try {
    const url = `/api/search?query=${encodeURIComponent(query)}&cookie=${encodeURIComponent(cookie)}`;
    const resp = await fetch(url);
    const data = await resp.json();

    if (data.error) {
      document.getElementById("content").innerHTML = `
        <div class="empty"><div class="icon">😢</div><div>${data.error}</div></div>
      `;
      return;
    }

    currentData = data;
    currentData.video_limited = data.video_limited || false;
    currentUid = data.user && data.user.uid ? String(data.user.uid) : "";
    videoTotal = data.video_total || data.videos.length;
    currentVideoPage = 1;
    // 动态分页状态初始化（首屏后端已拉好第1页）
    currentDynPage = 1;
    dynHasMore = !!data.dyn_has_more;
    dynLoaded = data.dyn_loaded || 0;
    dynImageText = data.dyn_image_text || 0;
    dynTypeFilter = restoreDynFilter();            // 恢复上次筛选状态
    dynTypeCounts = data.dyn_type_counts || {};   // 整个缓冲的类型计数（筛选栏用）
    dynFilteredLoaded = dynLoaded;
    selectedVideos.clear();
    selectedDynamics.clear();
    renderUser(data.user);
    renderCounts(videoTotal, dynLoaded);
    document.getElementById("tabs").style.display = "flex";
    switchTab(currentTab);

    // 搜索成功后启用「收藏当前」按钮
    const addBtn = document.getElementById("favUpAddBtn");
    if (addBtn) addBtn.disabled = !(currentData && currentData.user && currentData.user.uid);

    // 如果有部分失败的警告，在顶部显示提示
    if (data.warnings && data.warnings.length > 0) {
      const warnDiv = document.createElement("div");
      warnDiv.style.cssText = "padding:10px 15px;margin:10px 0;border-radius:8px;background:#1c2128;border:1px solid #f0883e;color:#f0883e;font-size:13px;";
      warnDiv.innerHTML = `<b>⚠ 部分功能受限：</b><br>${data.warnings.join("<br>")}<br><br>建议在右上角设置Cookie后重试。`;
      document.getElementById("content").insertBefore(warnDiv, document.getElementById("content").firstChild);
    }
  } catch (e) {
    document.getElementById("content").innerHTML = `
      <div class="empty"><div class="icon">💥</div><div>请求失败: ${e.message}</div></div>
    `;
  } finally {
    btn.disabled = false;
    btn.textContent = "搜索";
  }
}

// ==================== 常用UP主 ====================
let favUps = [];  // [{uid, name, face}]，localStorage 持久化

function loadFavUps() {
  try {
    favUps = JSON.parse(localStorage.getItem("bilibili_fav_ups") || "[]");
  } catch (e) {
    favUps = [];
  }
  renderFavUps();
}

function renderFavUps() {
  const box = document.getElementById("favUpList");
  if (!box) return;
  if (!favUps.length) {
    box.innerHTML = `<span class="fav-up-empty">还没有收藏的UP主，搜索后点「★ 收藏当前」即可加入</span>`;
    return;
  }
  box.innerHTML = favUps.map((u) => {
    const uid = String(u.uid);
    const init = (u.name || "?").slice(0, 1);
    const face = u.face
      ? `<img class="fav-up-avatar" src="${u.face}" onerror="this.style.visibility='hidden'">`
      : `<span class="fav-up-avatar" style="display:flex;align-items:center;justify-content:center;background:#fb7299;color:#fff;font-size:12px;">${escapeHtml(init)}</span>`;
    const name = escapeHtml(u.name || ("UID:" + uid));
    return `<div class="fav-up-item" onclick="searchByQuery('${uid}')" title="${name}">
      ${face}
      <span class="fav-up-name">${name}</span>
      <span class="fav-up-del" onclick="event.stopPropagation();removeFavUp('${uid}')" title="移除">×</span>
    </div>`;
  }).join("");
}

function addCurrentUp() {
  if (!currentData || !currentData.user || !currentData.user.uid) {
    alert("请先搜索一个UP主");
    return;
  }
  const u = currentData.user;
  const uid = String(u.uid);
  // 去重（按 uid）并置顶
  favUps = favUps.filter((x) => String(x.uid) !== uid);
  favUps.unshift({ uid, name: u.name || ("UID:" + uid), face: u.face || "" });
  if (favUps.length > 30) favUps = favUps.slice(0, 30);  // 最多保留 30 个
  localStorage.setItem("bilibili_fav_ups", JSON.stringify(favUps));
  renderFavUps();
}

function removeFavUp(uid) {
  favUps = favUps.filter((x) => String(x.uid) !== String(uid));
  localStorage.setItem("bilibili_fav_ups", JSON.stringify(favUps));
  renderFavUps();
}

// ==================== 渲染 ====================

// 秒级时间戳 → "2026年7月26日18时35分"（时/分零补齐，年/月/日不补）
function formatBiliDateTime(ts) {
  const dt = new Date(ts * 1000);
  const y = dt.getFullYear();
  const mo = dt.getMonth() + 1;
  const d = dt.getDate();
  const h = String(dt.getHours()).padStart(2, "0");
  const mi = String(dt.getMinutes()).padStart(2, "0");
  return `${y}年${mo}月${d}日${h}时${mi}分`;
}

function renderUser(user) {
  document.getElementById("userCard").style.display = "block";
  // 头像：有图则显示；加载失败或无图则回退到首字母占位，避免出现空白/破图
  const avatar = document.getElementById("userAvatar");
  const avatarFallback = document.getElementById("userAvatarFallback");
  if (user.face) {
    avatar.src = user.face;
    avatar.style.display = "block";
    if (avatarFallback) avatarFallback.style.display = "none";
  } else {
    avatar.style.display = "none";
    if (avatarFallback) {
      avatarFallback.textContent = (user.name || "?").trim().charAt(0);
      avatarFallback.style.display = "flex";
    }
  }
  document.getElementById("userName").textContent = user.name;
  const uidEl = document.getElementById("userUid");
  if (uidEl) uidEl.textContent = (user.uid != null && user.uid !== 0) ? "UID: " + user.uid : "";
  document.getElementById("userSign").textContent = user.sign || "";
  // 等级徽章：按需求不展示用户等级，始终隐藏
  const lvEl = document.getElementById("userLevel");
  if (lvEl) lvEl.style.display = "none";
  // 最新投稿：格式 = 「最新投稿 - 完整日期 - 标题」
  const lp = document.getElementById("userLastPost");
  if (lp) lp.textContent = user.last_post ? " - " + formatBiliDateTime(user.last_post) : " -";
  const lpt = document.getElementById("userLastPostTitle");
  if (lpt) {
    if (user.last_post_title) {
      lpt.textContent = " - " + user.last_post_title;
      if (user.last_post_url) {
        lpt.classList.add("clickable");
        lpt.title = "点击直达该投稿";
        lpt.onclick = () => window.open(user.last_post_url, "_blank", "noopener");
      } else {
        lpt.classList.remove("clickable");
        lpt.title = "";
        lpt.onclick = null;
      }
    } else {
      lpt.textContent = "";
      lpt.classList.remove("clickable");
      lpt.title = "";
      lpt.onclick = null;
    }
  }
}

function renderCounts(videoCount, dynamicCount) {
  const vc = document.getElementById("videoCount");
  if (vc) vc.textContent = videoCount; // 视频标签已移除，元素可能不存在
  const dc = document.getElementById("dynamicCount");
  if (dc) dc.textContent = dynamicCount; // 「全部投稿」Tab 计数
}

function switchTab(tab) {
  currentTab = tab;
  // 点击「全部投稿」Tab 时，若正处于某类型筛选，则回到"全部"视图（该 Tab 即代表全部）
  // 仅当用户主动点 Tab 时触发；doSearch 已先把 dynTypeFilter 重置为"全部"，不会误触发
  if (tab === "dynamics" && dynTypeFilter !== "全部") {
    dynTypeFilter = "全部";
    loadDynPage(1, true);
    return;
  }
  // 更新Tab样式
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === tab);
  });

  // 显示/隐藏全选框（视频或动态有内容时显示）
  const selectAllArea = document.getElementById("selectAllArea");
  if (selectAllArea) {
    const hasContent = tab === "videos"
      ? (videoTotal > 0)
      : (dynLoaded > 0);
    selectAllArea.style.display = hasContent ? "flex" : "none";
  }

  const content = document.getElementById("content");
  if (!currentData) return;

  if (tab === "videos") {
    content.innerHTML = renderVideos(currentData.videos);
  } else {
    content.innerHTML = renderDynamics(currentData.dynamics);
  }

  // 更新全选框状态
  updateSelectAllCheckbox();
  // 更新已选个数
  updateSelectedCount();
}

// 仅重渲染当前 Tab 内容：保留当前 dynTypeFilter，不重置筛选、不跳 Tab
// 注意：switchTab 在 dynamics 且 dynTypeFilter!=="全部" 时会把筛选重置为"全部"并重新加载，
// 因此"重渲染同一 Tab"（如全选/单一下载完成/历史移除）必须用本函数，否则筛选会被清掉跳回全部投稿
function rerenderCurrentTab() {
  const content = document.getElementById("content");
  if (!content || !currentData) return;
  if (currentTab === "videos") {
    content.innerHTML = renderVideos(currentData.videos);
  } else {
    content.innerHTML = renderDynamics(currentData.dynamics);
  }
  updateSelectAllCheckbox();
  updateSelectedCount();
}

function renderVideos(videos) {
  if (!videos || videos.length === 0) {
    return `<div class="empty"><div class="icon">📭</div><div>该UP主没有视频</div></div>`;
  }
  const cards = videos
    .map((v) => {
      const date = v.created
        ? new Date(v.created * 1000).toLocaleDateString("zh-CN")
        : "";
      videoTitleById[v.bvid] = v.title;
      const isDl = v.downloaded || isDownloadedCheck("0", v.bvid);
      const chargeBadge = v.charge_only ? `<span class="charge-badge">🔋 充电专属</span>` : "";
      const dlBtn = isDl
        ? `<button class="btn-download downloaded" disabled>已下载</button>`
        : `<button class="btn-download" onclick='downloadVideo(${jsAttr(v.bvid)}, ${jsAttr(v.title)}, undefined, false, undefined, getFolderDynType(v))'>下载视频</button>`;
      const direct = v.bvid
        ? `<a class="btn-direct" href="https://www.bilibili.com/video/${encodeURIComponent(v.bvid)}" target="_blank" rel="noopener">直达</a>`
        : "";
      // 分P按钮：默认隐藏，revealMultiP 在后台探测到多P后才显示（单P不显示）
      const pagesBtn = `<button class="btn-pages" style="display:none" data-bvid="${v.bvid}" data-kind="v" data-key="${v.bvid}" onclick="openPagesModal('v','${v.bvid}','${v.bvid}')">分P</button>`;
      // 复选框（已下载的不显示）
      const checkbox = isDl
        ? ""
        : `<input type="checkbox" class="item-checkbox" data-bvid="${v.bvid}" ${selectedVideos.has(v.bvid) ? "checked" : ""} onchange="onVideoCheck('${v.bvid}', this.checked)">`;
      return `
      <div class="card video-card">
        <div class="check-col">${checkbox}</div>
        <div class="vc-cover-wrap">
          <img class="thumb" src="${v.cover}" alt="" loading="lazy"
               onerror="this.style.opacity=0.3">
          ${chargeBadge}
        </div>
        <div class="info">
          <div class="dy-content"><div class="vtitle">${escapeHtml(v.title)}</div></div>
          <div class="meta">
            <span>▶ ${formatNum(v.play)}</span>
            <span>⏱ ${v.duration}</span>
            <span>📅 ${date}</span>
          </div>
          <div class="dy-actions">${pagesBtn}${direct}${dlBtn}</div>
        </div>
      </div>`;
    })
    .join("");

  // 分页控件
  const totalPages = Math.ceil(videoTotal / videoPerPage);
  const pagination = renderPagination(currentVideoPage, totalPages, "loadVideoPage");
  // 后台探测哪些视频是多P，仅对多P显示 分P 按钮（microtask 在本次渲染写入 DOM 后执行）
  queueMicrotask(revealMultiP);
  return `<div class="video-grid">${cards}</div>` + pagination;
}

// 联合投稿抓取关键词（与 server.py JOINT_SUBMISSION_KEYWORDS 一致）
const JOINT_KEYWORDS = ["合作视频", "联合投稿"];
function isJointSubmission(d) {
  const blob = `${d.title || ""}\n${d.text || ""}`;
  return JOINT_KEYWORDS.some((kw) => blob.includes(kw));
}
// 真实类型标签（不含联合投稿覆盖），用于服务端类型筛选 / 全选匹配，保持与 server._dyn_type_label 一致
function getDynRealType(d) {
  if (d.charge_only) return "充电专属";
  if (d.type_label === "视频" && d.dyn_video_type) return d.dyn_video_type;
  return d.type_label;
}
// 计算动态的类型徽章文字（与 dy-type 徽章一致）：充电专属 > 联合投稿 > 真实类型
function getDynTypeLabel(d) {
  if (d.charge_only) return "充电专属";
  if (isJointSubmission(d)) return "联合投稿";
  if (d.type_label === "视频" && d.dyn_video_type) return d.dyn_video_type;
  return d.type_label;
}
// 判断某条动态是否匹配指定筛选（联合投稿为互斥分类：联合动态不归入真实类型筛选）
function dynMatchesFilter(d, filter) {
  if (filter === "联合投稿") return isJointSubmission(d);
  if (isJointSubmission(d)) return false; // 联合投稿互斥：联合动态只在「联合投稿」筛选里出现
  return getDynRealType(d) === filter;
}
// 下载类型标签：联合投稿作为独立下载分类，与真实类型正交（充电专属仍最高优先级）
// 用于 checkDownloadType / 批量下载 / 下载全部 的下载许可判定。
function getDynDownloadLabel(d) {
  // 和卡片显示的 typeLabel 保持一致，直接用 getDynTypeLabel
  return getDynTypeLabel(d);
}

// 点击筛选按钮：TAB 级筛选——由服务端在整个已加载缓冲里过滤并重新分页（不是只筛当前页）
function setDynFilter(label) {
  // 再次点击当前已激活的胶囊 → 取消筛选，回到「全部」（避免筛选"卡住"无法退出）
  if (dynTypeFilter === label) {
    dynTypeFilter = "全部";
    updateDynFilterState();
    loadDynPage(1, true);
    return;
  }
  dynTypeFilter = label;
  updateDynFilterState();
  loadDynPage(1, true); // 切换筛选后回到第 1 页（强制请求，跳过"没有更多"守卫）
}

// 记住筛选状态到浏览器存储 + 同步 URL hash
function updateDynFilterState() {
  try {
    sessionStorage.setItem("dynTypeFilter", dynTypeFilter);
  } catch (e) { /* 忽略 */ }
  // URL hash 锚点同步（方便从外部直达）
  if (dynTypeFilter && dynTypeFilter !== "全部") {
    location.hash = encodeURIComponent(dynTypeFilter);
  } else {
    history.replaceState(null, "", location.pathname + location.search);
  }
}

// 读取筛选状态
function restoreDynFilter() {
  // 优先 URL hash（外部直达），其次浏览器存储
  let filter = "全部";
  if (location.hash) {
    try {
      filter = decodeURIComponent(location.hash.slice(1));
    } catch (e) { /* 忽略 */ }
  } else {
    try {
      filter = sessionStorage.getItem("dynTypeFilter") || "全部";
    } catch (e) { /* 忽略 */ }
  }
  return filter;
}

// ===== 分类单一数据源：TAB 栏筛选胶囊 与 下载类型复选框 都从这里派生 =====
// 修改这里一处，TAB 栏和下载设置会自动同步，无需再改两处。
// 「转发」既用于 TAB 筛选也是可下载分类；分类列表与 TAB 栏完全一致（无「未知」兜底）。
const DYN_CATEGORIES = ["投稿视频", "动态视频", "充电专属", "图文", "文字", "转发", "联合投稿", "未知"];

// 下载类型 = TAB 栏分类完全一致（同一份 DYN_CATEGORIES，含「转发」「未知」）
function getDownloadTypeCategories() {
  return [...DYN_CATEGORIES];
}

// 动态渲染下载类型复选框（从 DYN_CATEGORIES 派生，与 TAB 栏同步）
function renderDownloadTypeCheckboxes(selectedTypes) {
  const group = document.getElementById("typeCheckGroup");
  if (!group) return;
  const cats = getDownloadTypeCategories();
  const sel = new Set(selectedTypes || []); // 未传则默认不勾选
  group.innerHTML = cats.map((c) =>
    `<label class="type-check-item"><input type="checkbox" value="${c}" ${sel.has(c) ? "checked" : ""}> ${c}</label>`
  ).join("");
}

function renderDynamics(dynamics) {
  dynamics = dynamics || [];
  // ===== 类型筛选栏（TAB 级）：计数来自整个已加载缓冲（服务端 type_counts），不是本页 =====
  const typeCounts = dynTypeCounts || {};
  // 动态展示：只显示当前缓冲里 count>0 的类型（避免 0 计数胶囊占位；类型随加载逐步出现）
  // 顺序取自单一数据源 DYN_CATEGORIES（与下载类型复选框同步）
  const preferredOrder = DYN_CATEGORIES;
  // 排除服务端可能漏出的退化标签（如空 dyn_video_type 退化出的「视频」、缺 type_label 的「其他」）
  const hiddenLabels = new Set(["视频", "其他"]);
  const labels = preferredOrder
    .filter((l) => !hiddenLabels.has(l) && (typeCounts[l] || 0) > 0)
    .concat(
      Object.keys(typeCounts).filter(
        (l) => !preferredOrder.includes(l) && !hiddenLabels.has(l) && (typeCounts[l] || 0) > 0
      )
    );
  // 当前选中的筛选即使本批缓冲计数为 0 也保留入口（避免高亮消失、可一键切回"全部"）
  if (dynTypeFilter !== "全部" && !labels.includes(dynTypeFilter)) {
    labels.unshift(dynTypeFilter);
  }
  // 筛选按钮渲染到 Tab 栏「全部投稿」右边的 #dynFilterTabs 容器（不占内容区）
  const filterTabs = document.getElementById("dynFilterTabs");
  if (filterTabs) {
    if (labels.length === 0) {
      filterTabs.style.display = "none"; // 无任何有效类型时隐藏整条筛选栏
    } else {
      filterTabs.innerHTML = labels.map((label) => {
        const count = typeCounts[label] != null ? typeCounts[label] : 0;
        const active = dynTypeFilter === label ? " active" : "";
        return `<button class="dyn-filter-btn${active}" onclick="setDynFilter('${label}')">${label} <span class="f-count">${count}</span></button>`;
      }).join("");
      filterTabs.style.display = "flex"; // 仅动态标签激活时显示（renderDynamics 只在动态视图调用）
    }
  }
  // 主「全部投稿」Tab 仅在未选子筛选时高亮；选中某类型筛选时仅高亮对应胶囊，避免同时两个 active
  const mainTab = document.querySelector('.tab[data-tab="dynamics"]');
  if (mainTab) mainTab.classList.toggle("active", dynTypeFilter === "全部");
  // 本页数据已由服务端按筛选类型过滤好，直接渲染
  const shown = dynamics;

  const cards = shown
    .map((d) => {
      const images = (d.images || [])
        .slice(0, 9)
        .map((url) => `<img src="${url}" loading="lazy" onclick="previewImage('${url}')">`)
        .join("");
      // 标题也走表情渲染：opus 图文的标题里可能含 [表情名] 占位符或 Unicode emoji
      const titleHtml = d.title ? `<div class="dy-title">${renderTextWithEmoji(d.title, d.emoji_map)}</div>` : "";
      const dyTextRaw = (d.text || "").trim();
      const hasText = dyTextRaw && dyTextRaw !== "-" && d.text !== d.title;
      // 显示规则：标题+正文都有→只显示标题；只有正文→显示正文；都没有→空白
      const textHtml = (!d.title && hasText)
        ? `<div class="dy-text">${renderTextWithEmoji(d.text, d.emoji_map)}</div>`
        : "";
      // 视频动态显示封面图；充电专属在封面右上角加角标（模仿 B站 封面右上角标志）
      const durationHtml = (d.type === "video" && d.duration)
        ? `<span class="dy-duration">${formatDuration(d.duration)}</span>` : "";
      const coverHtml = (d.type === "video" && d.cover)
        ? `<div class="dy-cover-wrap">
             <img class="dy-cover" src="${d.cover}" loading="lazy" onclick="previewImage('${d.cover}')" onerror="this.style.display='none'">
             ${d.charge_only ? `<span class="dy-cover-badge">充电专属</span>` : ""}
             ${durationHtml}
           </div>`
        : "";
      // 动态视频卡片：除动态历史(dynamic id)外，若其 bvid 已在 video 历史中（任何路径下载过，
      // 含自动下载只写了 video 历史的旧数据），也应视为已下载，保证视频/动态 Tab 状态一致
      const isDl = d.downloaded || isDownloadedCheck(d.id, d.bvid)
        || (d.bvid && isDownloadedCheck("0", d.bvid));
      dynById[d.id] = d;
      // 类型徽章：充电专属优先（把"投稿视频"替换为"充电专属"）；否则视频动态显示 动态视频/投稿视频；其余用默认标签
      const typeLabel = getDynTypeLabel(d);
      const dyTypeCls = d.charge_only ? "charge" : d.type;
      const canDownload = ["video", "image", "text"].includes(d.type);
      const dlBtnText = "下载";
      const dlBtn = isDl
        ? `<button class="btn-download downloaded" disabled>已下载</button>`
        : (canDownload
            ? `<button class="btn-download" onclick='downloadDynamic(${JSON.stringify(d).replace(/'/g, "&#39;")})'>${dlBtnText}</button>`
            : "");
      // 直达按钮：跳转到原动态页（t.bilibili.com/{动态ID}），新标签页打开
      const directUrl = d.id ? `https://t.bilibili.com/${encodeURIComponent(d.id)}` : "";
      const directBtn = directUrl
        ? `<a class="btn-direct" href="${directUrl}" target="_blank" rel="noopener">直达</a>`
        : "";
      // 复选框
      const checkbox = (isDl || !canDownload)
        ? `<span class="item-checkbox-placeholder"></span>`  /* 占位保持布局对齐，避免徽章随是否有复选框偏移 */
        : `<input type="checkbox" class="item-checkbox" data-dyid="${d.id}" ${selectedDynamics.has(d.id) ? "checked" : ""} onchange="onDynamicCheck('${d.id}', this.checked)">`;
      // 视频动态支持分P（懒加载，仅多P才显示按钮）
      const pagesBtn = (d.type === "video" && d.id && d.bvid)
        ? `<button class="btn-pages" style="display:none" data-bvid="${d.bvid}" data-kind="d" data-key="${d.id}" onclick="openPagesModal('d','${d.id}','${d.bvid}')">分P</button>` : "";

      return `
      <div class="card dynamic-card">
        <div class="dy-header-wrap"><div class="dy-header">
          ${checkbox}
          <span class="dy-type ${dyTypeCls}">${typeLabel}</span>
          <span class="dy-time">${d.time_str}</span>
        </div></div>
        ${titleHtml || textHtml ? `<div class="dy-title-wrap">${titleHtml}${textHtml}</div>` : ""}
        ${coverHtml}
        ${images ? `<div class="dy-images-wrap"><div class="dy-images">${images}</div></div>` : ""}
        <div class="dy-actions-wrap"><div class="dy-actions">
          ${pagesBtn}
          ${directBtn}
          ${dlBtn}
        </div></div>
      </div>`;
    })
    .join("");
  // 分页控件（每页 dynPerPage 条，点下一页才向后拉一批）
  // 顶部 + 底部都放一份，顶部无需下拉即可翻页；输入 id 需唯一避免冲突
  // 筛选状态下按「筛选后的总条数」分页显示
  const effLoaded = dynTypeFilter === "全部" ? dynLoaded : dynFilteredLoaded;
  const topPagination = renderDynPagination(currentDynPage, dynHasMore, effLoaded, "dynPageInputTop");
  const bottomPagination = renderDynPagination(currentDynPage, dynHasMore, effLoaded, "dynPageInput");
  const gridHtml = shown.length > 0
    ? `<div class="dyn-grid">${cards}</div>`
    : (dynHasMore
        ? `<div class="empty"><div class="icon">🔍</div><div>已加载的 ${dynLoaded} 条里暂无更多「${dynTypeFilter}」，点「下一页」继续向后查找</div></div>`
        : `<div class="empty"><div class="icon">🔍</div><div>没有「${dynTypeFilter}」类型的动态</div></div>`);
  // 后台探测哪些视频动态是多P，仅对多P显示 分P 按钮
  queueMicrotask(revealMultiP);
  return topPagination + gridHtml + bottomPagination;
}

// ==================== 分页 ====================

function renderPagination(current, total, loadFunc) {
  if (total <= 1) return "";
  let html = `<div class="pagination">`;
  // 上一页
  html += `<button class="page-btn" onclick="${loadFunc}(${current - 1})" ${current <= 1 ? "disabled" : ""}>‹ 上一页</button>`;
  // 页码（最多显示7个，中间用省略号）
  const pages = getPageNumbers(current, total);
  for (const p of pages) {
    if (p === "...") {
      html += `<span class="page-ellipsis">···</span>`;
    } else if (p === current) {
      html += `<button class="page-btn active">${p}</button>`;
    } else {
      html += `<button class="page-btn" onclick="${loadFunc}(${p})">${p}</button>`;
    }
  }
  // 下一页
  html += `<button class="page-btn" onclick="${loadFunc}(${current + 1})" ${current >= total ? "disabled" : ""}>下一页 ›</button>`;
  // 总数信息
  html += `<span class="page-info">共 ${total} 页</span>`;
  html += `</div>`;
  return html;
}

function getPageNumbers(current, total) {
  if (total <= 7) {
    return Array.from({length: total}, (_, i) => i + 1);
  }
  if (current <= 4) {
    return [1, 2, 3, 4, 5, "...", total];
  }
  if (current >= total - 3) {
    return [1, "...", total - 4, total - 3, total - 2, total - 1, total];
  }
  return [1, "...", current - 1, current, current + 1, "...", total];
}

function renderDynPagination(current, hasMore, loaded, inputId) {
  inputId = inputId || "dynPageInput";
  let html = `<div class="pagination">`;
  // 首页
  html += `<button class="page-btn" onclick="loadDynPage(1)" ${current <= 1 ? "disabled" : ""}>« 首页</button>`;
  // 上一页
  html += `<button class="page-btn" onclick="loadDynPage(${current - 1})" ${current <= 1 ? "disabled" : ""}>‹ 上一页</button>`;
  const tail = hasMore ? "" : "（已全部加载）";
  const loadedLabel = dynTypeFilter === "全部" ? "已缓冲" : `「${dynTypeFilter}」已找到`;
  // 已全部加载时不显示「每页 N 条」，并加深字体
  const perPageText = hasMore ? ` · 每页 ${dynPerPage} 条` : "";
  const infoClass = hasMore ? "page-info" : "page-info all-loaded";
  const estPages = Math.ceil(loaded / dynPerPage);
  html += `<span class="${infoClass}">第 ${current} 页${perPageText} · ${loadedLabel} ${estPages}页${tail}</span>`;
  // 跳转到指定页
  html += `<input type="number" class="page-input" id="${inputId}" min="1" placeholder="页" onkeydown="if(event.key==='Enter') jumpDynPage('${inputId}')" />`;
  html += `<button class="page-btn" onclick="jumpDynPage('${inputId}')">跳转</button>`;
  // 下一页
  html += `<button class="page-btn" onclick="loadDynPage(${current + 1})" ${hasMore ? "" : "disabled"}>下一页 ›</button>`;
  html += `</div>`;
  return html;
}

function jumpDynPage(inputId) {
  inputId = inputId || "dynPageInput";
  const input = document.getElementById(inputId);
  if (!input) return;
  let p = parseInt(input.value, 10);
  if (isNaN(p) || p < 1) return;
  loadDynPage(p);
}

async function loadDynPage(page, force) {
  if (!currentUid || page < 1) return;
  // 没有更多且已到末尾时不再请求（按当前筛选视图的条数判断；force=切换筛选时跳过）
  const effLoaded = dynTypeFilter === "全部" ? dynLoaded : dynFilteredLoaded;
  if (!force && !dynHasMore && page * dynPerPage > effLoaded) return;
  const content = document.getElementById("content");
  content.innerHTML = `<div class="loading"><div class="spinner"></div>正在加载第 ${page} 页动态...</div>`;
  try {
    const dtypeParam = dynTypeFilter !== "全部" ? `&dtype=${encodeURIComponent(dynTypeFilter)}` : "";
    const url = `/api/dynamics?uid=${encodeURIComponent(currentUid)}&page=${page}${dtypeParam}&cookie=${encodeURIComponent(cookie)}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) {
      content.innerHTML = `<div class="empty"><div class="icon">💥</div><div>${data.error}</div></div>`;
      return;
    }
    if (!data.dynamics || data.dynamics.length === 0) {
      if (data.has_more) {
        // 缓冲还有更多动态没拉完，该稀少类型可能埋在更深处：提示继续查找而非假"没有更多"
        const tip = dynTypeFilter === "全部"
          ? `已加载 ${data.loaded} 条，点「下一页」继续加载`
          : `已加载 ${data.loaded} 条里暂未找到「${dynTypeFilter}」类型的动态，点「下一页」继续向后查找`;
        content.innerHTML = `<div class="empty"><div class="icon">🔍</div><div>${tip}</div></div>`;
      } else {
        content.innerHTML = `<div class="empty"><div class="icon">📭</div><div>${dynTypeFilter === "全部" ? "没有更多动态了" : "没有「" + dynTypeFilter + "」类型的动态"}</div></div>`;
      }
      return;
    }
    // 更新动态分页状态（筛选状态下本页可能为空——继续渲染筛选栏和"继续查找"提示）
    currentData.dynamics = data.dynamics || [];
    currentDynPage = page;
    dynHasMore = data.has_more;
    dynLoaded = data.loaded;
    dynImageText = data.image_text;
    dynFilteredLoaded = data.filtered_loaded != null ? data.filtered_loaded : data.loaded;
    if (data.type_counts) dynTypeCounts = data.type_counts;
    // 标记已下载（服务端历史 + 本地集合）
    for (const d of data.dynamics) {
      d.downloaded = d.downloaded || isDownloadedCheck(d.id, d.bvid);
    }
    // 同步 Tab 栏动态数（累计已加载）
    const dc = document.getElementById("dynamicCount");
    if (dc) dc.textContent = dynLoaded;
    content.innerHTML = renderDynamics(currentData.dynamics);
    updateSelectAllCheckbox();
  } catch (e) {
    content.innerHTML = `<div class="empty"><div class="icon">💥</div><div>加载失败: ${e.message}</div></div>`;
  }
}

async function loadVideoPage(page) {
  if (!currentUid || page < 1) return;
  const totalPages = Math.ceil(videoTotal / videoPerPage);
  if (page > totalPages) return;

  // 显示加载中
  const content = document.getElementById("content");
  content.innerHTML = `<div class="loading"><div class="spinner"></div>正在加载第 ${page} 页...</div>`;

  try {
    const url = `/api/videos?uid=${encodeURIComponent(currentUid)}&page=${page}&cookie=${encodeURIComponent(cookie)}`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) {
      content.innerHTML = `<div class="empty"><div class="icon">💥</div><div>${data.error}</div></div>`;
      return;
    }
    // 更新当前页的视频列表（保留其他数据不变）
    currentData.videos = data.videos;
    currentVideoPage = page;
    // 标记已下载状态
    for (const v of data.videos) {
      v.downloaded = v.downloaded || isDownloadedCheck("0", v.bvid);
    }
    content.innerHTML = renderVideos(currentData.videos);
    setupContentToggles(content);
    // 更新全选框状态
    updateSelectAllCheckbox();
  } catch (e) {
    content.innerHTML = `<div class="empty"><div class="icon">💥</div><div>加载失败: ${e.message}</div></div>`;
  }
}

// ==================== 下载 ====================

function checkDownloadType(label, isCharge, silent) {
  // 下载类型过滤：首次未设置则全部拦截，需在下载设置中勾选后生效
  const types = (localStorage.getItem("bili_dl_types") || "").split(",").filter(Boolean);
  if (isCharge && !types.includes("充电专属")) {
    if (!silent) alert("充电专属已被禁止下载（可在下载设置里修改）");
    return false;
  }
  if (isCharge) return true;
  if (!types.includes(label)) {
    if (!silent) alert(`"${label}" 已被禁止下载（可在下载设置里修改）`);
    return false;
  }
  return true;
}

// 动态视频下载时，folder 命名模板的 dynType 取值（与 dy-type 显示一致）
function getFolderDynType(v) {
  const lbl = getDynTypeLabel(v);
  if (lbl === "转发") return "转发";
  if (lbl === "动态视频") return "动态视频";
  return "普通视频"; // 投稿视频动态 / 视频Tab 投稿视频
}

async function downloadVideo(bvid, title, username, silent, page, dynType, source = null, favName = "") {
  let isCharge = false;
  if (currentData && currentData.videos) {
    const v = currentData.videos.find(x => x.bvid === bvid);
    if (v) isCharge = v.charge_only;
  }
  if (!checkDownloadType("投稿视频", isCharge, silent)) return false;
  username = username || (currentData ? currentData.user.name : "未知UP主");
  try {
    const payload = { bvid, title, username, cookie, uid: currentData ? currentData.user.uid : "", qn: getVideoQn() };
    if (dynType) payload.dynType = dynType;
    if (source) { payload.source = source; payload.self_name = selfInfo.name || "我"; }
    if (source === "favorites") { payload.fav_name = favName || ""; }
    if (page) payload.page = page;
    const resp = await fetch("/api/download/video", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.error) {
      alert(data.error);
      return false;
    }
    if (data.already_downloaded) {
      // 已下载过，标记并刷新列表
      markDownloaded("0", bvid);
      rerenderCurrentTab();
      return false;
    }
    // 记录任务ID→项目映射，下载成功后才标记
    if (data.task_id) {
      taskItemMap[data.task_id] = { type: "video", id: bvid, title };
    }
    toggleDownloadPanel(true);
    startPolling();
    return true;
  } catch (e) {
    alert("下载请求失败: " + e.message);
    return false;
  }
}

async function downloadDynamic(dynamic, silent, page) {
  // 映射到下载类型标签（联合投稿为独立下载分类，和 TAB 栏筛选胶囊一致）
  const label = getDynDownloadLabel(dynamic);
  if (!checkDownloadType(label, dynamic.charge_only, silent)) return false;
  const username = currentData ? currentData.user.name : "未知UP主";
  try {
    const payload = { dynamic, username, cookie, uid: currentData ? currentData.user.uid : "", qn: getVideoQn() };
    if (page) payload.page = page;
    const resp = await fetch("/api/download/dynamic", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.error) {
      alert(data.error);
      return false;
    }
    if (data.already_downloaded) {
      markDownloaded(dynamic.id, "0");
      rerenderCurrentTab();
      return false;
    }
    // 记录任务ID→项目映射，下载成功后才标记
    if (data.task_id) {
      taskItemMap[data.task_id] = { type: "dynamic", id: dynamic.id, bvid: dynamic.bvid || "0", title: dynamic.title || dynamic.text?.slice(0, 30) };
    }
    toggleDownloadPanel(true);
    startPolling();
    return true;
  } catch (e) {
    alert("下载请求失败: " + e.message);
    return false;
  }
}

// ==================== 多P（分P）弹窗 + 多P检测 ====================
// 卡片标题/动态对象的缓存，避免在 onclick 里传递含引号的字符串
const videoTitleById = {};
const dynById = {};
// 分P信息缓存：{bvid: {count, pages, title}}（来自 /api/video_page_counts 批量或 /api/video_pages 单条）
const pageDataCache = {};

// 打开分P弹窗：优先用缓存，否则拉取 /api/video_pages
async function openPagesModal(kind, key, bvid) {
  const overlay = document.getElementById("pagesModal");
  const body = document.getElementById("pagesModalBody");
  const titleEl = document.getElementById("pagesModalTitle");
  if (!overlay || !body) return;
  const cached = pageDataCache[bvid];
  if (cached && cached.pages && cached.pages.length > 1) {
    titleEl.textContent = "📂 " + (cached.title || bvid);
    renderPagesModal(body, kind, key, bvid, cached);
    overlay.classList.add("show");
    return;
  }
  body.innerHTML = '<div class="pages-loading">加载分P中…</div>';
  titleEl.textContent = "📂 " + (videoTitleById[bvid] || bvid);
  overlay.classList.add("show");
  try {
    const resp = await fetch("/api/video_pages?bvid=" + encodeURIComponent(bvid));
    const data = await resp.json();
    if (data.error) { body.innerHTML = '<div class="pages-err">' + escapeHtml(data.error) + "</div>"; return; }
    if (!data.pages || data.pages.length <= 1) {
      body.innerHTML = '<div class="pages-err">该视频为单P，无需展开</div>';
      return;
    }
    pageDataCache[bvid] = data;
    titleEl.textContent = "📂 " + (data.title || bvid);
    renderPagesModal(body, kind, key, bvid, data);
  } catch (e) {
    body.innerHTML = '<div class="pages-err">加载失败：' + escapeHtml(e.message) + "</div>";
  }
}

function closePagesModal() {
  const overlay = document.getElementById("pagesModal");
  if (overlay) overlay.classList.remove("show");
}

function renderPagesModal(body, kind, key, bvid, data) {
  const rows = data.pages.map((p) => {
    const dur = p.duration ? formatDuration(p.duration) : "";
    const part = p.part || ("P" + p.page);
    const dlOnclick = kind === "v"
      ? `downloadVideo('${bvid}', videoTitleById['${bvid}'], undefined, false, ${p.page})`
      : `downloadDynamicPage('${key}', ${p.page})`;
    return `<div class="pages-row">
      <span class="pages-idx">P${p.page}</span>
      <span class="pages-name">${escapeHtml(part)}</span>
      <span class="pages-dur">${dur}</span>
      <button class="btn-download-sm" onclick="${dlOnclick}">下载此集</button>
    </div>`;
  }).join("");
  const allOnclick = kind === "v"
    ? `downloadVideo('${bvid}', videoTitleById['${bvid}'])`
    : `downloadDynamicPage('${key}', 0)`;
  body.innerHTML =
    `<div class="pages-head">共 ${data.pages.length} 集 · <button class="btn-download-sm" onclick="${allOnclick}">下载全部</button></div>` + rows;
}

// 渲染列表后调用：批量探测当前可见视频是否为多P，仅对多P显示 分P 按钮
async function revealMultiP() {
  const btns = Array.from(document.querySelectorAll(".btn-pages[data-bvid]"));
  if (btns.length === 0) return;
  const toFetch = [];
  for (const btn of btns) {
    const bvid = btn.getAttribute("data-bvid");
    const info = pageDataCache[bvid];
    if (info && info.count != null) {
      btn.style.display = info.count > 1 ? "" : "none";
    } else {
      btn.style.display = "none";
      if (bvid && toFetch.indexOf(bvid) === -1) toFetch.push(bvid);
    }
  }
  if (toFetch.length === 0) return;
  try {
    const resp = await fetch("/api/video_page_counts?bvids=" + encodeURIComponent(toFetch.join(",")));
    const data = await resp.json();
    const map = data.bvids || {};
    for (const btn of btns) {
      const bvid = btn.getAttribute("data-bvid");
      const info = map[bvid];
      if (info && info.count != null) {
        pageDataCache[bvid] = info;
        btn.style.display = info.count > 1 ? "" : "none";
      } else {
        // 单P 或 解析失败：缓存为单P，避免重复请求
        pageDataCache[bvid] = { count: 1, pages: [] };
        btn.style.display = "none";
      }
    }
  } catch (e) {
    // 请求失败：保持隐藏即可
  }
}

async function downloadDynamicPage(dynId, page) {
  const d = dynById[dynId];
  if (!d) { alert("动态数据已失效，请刷新列表后再试"); return; }
  await downloadDynamic(d, false, page || null);
}

// ==================== 复选框/全选 ====================

function onVideoCheck(bvid, checked) {
  if (checked) {
    selectedVideos.add(bvid);
  } else {
    selectedVideos.delete(bvid);
  }
  updateSelectAllCheckbox();
  updateSelectedCount();
}

function onDynamicCheck(dyid, checked) {
  if (checked) {
    selectedDynamics.add(dyid);
  } else {
    selectedDynamics.delete(dyid);
  }
  updateSelectedCount();
}

function toggleSelectAll(checked) {
  if (currentTab === "videos") {
    const videos = currentData.videos || [];
    if (checked) {
      // 全选未下载的视频
      videos.forEach(v => {
        if (!v.downloaded && !isDownloadedCheck("0", v.bvid)) {
          selectedVideos.add(v.bvid);
        }
      });
    } else {
      selectedVideos.clear();
    }
  } else {
    // 全选只作用于当前筛选类型下可见的动态（选了"充电专属"就只全选充电专属）
    let dynamics = currentData.dynamics || [];
    if (dynTypeFilter !== "全部") {
      dynamics = dynamics.filter(d => dynMatchesFilter(d, dynTypeFilter));
    }
    if (checked) {
      dynamics.forEach(d => {
        if (["video", "image", "text"].includes(d.type) && !d.downloaded && !isDownloadedCheck(d.id, d.bvid)) {
          selectedDynamics.add(d.id);
        }
      });
    } else {
      selectedDynamics.clear();
    }
  }
  rerenderCurrentTab();
}

function updateSelectAllCheckbox() {
  const cb = document.getElementById("selectAllCheckbox");
  if (!cb) return;
  if (currentTab === "videos") {
    const downloadable = (currentData.videos || []).filter(v => !v.downloaded && !isDownloadedCheck("0", v.bvid));
    cb.checked = downloadable.length > 0 && downloadable.every(v => selectedVideos.has(v.bvid));
    cb.indeterminate = !cb.checked && selectedVideos.size > 0;
  } else {
    let dyns = currentData.dynamics || [];
    if (dynTypeFilter !== "全部") {
      dyns = dyns.filter(d => dynMatchesFilter(d, dynTypeFilter));
    }
    const downloadable = dyns.filter(d => ["video","image","text"].includes(d.type) && !d.downloaded && !isDownloadedCheck(d.id, d.bvid));
    cb.checked = downloadable.length > 0 && downloadable.every(d => selectedDynamics.has(d.id));
    cb.indeterminate = !cb.checked && selectedDynamics.size > 0;
  }
}

function updateSelectedCount() {
  const el = document.getElementById("selectedCount");
  if (!el) return;
  const n = (currentTab === "videos") ? selectedVideos.size : selectedDynamics.size;
  el.textContent = n > 0 ? `已选 ${n} 个` : "";
}

// ==================== 批量下载（仅勾选项）====================

async function batchDownload() {
  if (!currentData) return;
  const btn = document.getElementById("batchBtn");

  if (currentTab === "videos") {
    // 获取勾选的视频
    let videos = (currentData.videos || []).filter(v => selectedVideos.has(v.bvid));
    if (videos.length === 0) {
      alert("请先勾选要下载的视频！");
      return;
    }
    // 按下载设置预过滤，被禁止的静默跳过并计数（避免逐条弹窗）
    let skipped = 0;
    videos = videos.filter(v => {
      if (checkDownloadType("投稿视频", !!v.charge_only, true)) return true;
      skipped++; return false;
    });
    if (videos.length === 0) {
      alert(`勾选的视频均被下载设置禁止下载（共 ${skipped} 条，可在下载设置里修改）`);
      return;
    }
    btn.disabled = true;
    btn.textContent = "下载中...";
    toggleDownloadPanel(true);
    startPolling();
    for (let i = 0; i < videos.length; i++) {
      const v = videos[i];
      btn.textContent = `下载中... (${i + 1}/${videos.length})`;
      await downloadVideo(v.bvid, v.title, undefined, true, undefined, getFolderDynType(v));
      selectedVideos.delete(v.bvid);
      if (i < videos.length - 1) await sleep(1000);
    }
    if (skipped > 0) alert(`已跳过 ${skipped} 条被禁止下载的视频（可在下载设置里修改）`);
  } else {
    // 获取勾选的动态
    let dynamics = (currentData.dynamics || []).filter(d => selectedDynamics.has(d.id));
    if (dynamics.length === 0) {
      alert("请先勾选要下载的动态！");
      return;
    }
    // 按下载设置预过滤，被禁止的静默跳过并计数（避免逐条弹窗）
    let skipped = 0;
    dynamics = dynamics.filter(d => {
      const label = getDynDownloadLabel(d);
      if (checkDownloadType(label, !!d.charge_only, true)) return true;
      skipped++; return false;
    });
    if (dynamics.length === 0) {
      alert(`勾选的动态均被下载设置禁止下载（共 ${skipped} 条，可在下载设置里修改）`);
      return;
    }
    btn.disabled = true;
    btn.textContent = "下载中...";
    toggleDownloadPanel(true);
    startPolling();
    for (let i = 0; i < dynamics.length; i++) {
      const d = dynamics[i];
      btn.textContent = `下载中... (${i + 1}/${dynamics.length})`;
      await downloadDynamic(d, true);
      selectedDynamics.delete(d.id);
      if (i < dynamics.length - 1) await sleep(1000);
    }
    if (skipped > 0) alert(`已跳过 ${skipped} 条被禁止下载的动态（可在下载设置里修改）`);
  }

  btn.disabled = false;
  btn.textContent = "批量下载";
  rerenderCurrentTab();
}

// ==================== 下载全部（分批 100 条，逐次推进）====================
// 每次点击从上次中断页继续向后搜索 100 条并下载，下回继续。

async function downloadAllDynamics() {
  if (!currentUid) {
    alert("请先搜索一个UP主");
    return;
  }
  const btn = document.getElementById("downloadAllBtn");
  if (!btn || btn.disabled) return;
  btn.disabled = true;

  // 1) 从第 1 页开始汇总，一直翻到凑满 100 条未下载的，或翻完为止
  btn.textContent = "汇总中...";
  const todo = [];
  let skipped = 0;
  let exhausted = false;
  let page = 1;
  try {
    while (todo.length < 100) {
      const url = `/api/dynamics?uid=${encodeURIComponent(currentUid)}&page=${page}&cookie=${encodeURIComponent(cookie)}`;
      const resp = await fetch(url);
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      const dyns = data.dynamics || [];
      if (dyns.length === 0) { exhausted = true; break; }
      for (const d of dyns) {
        if (todo.length >= 100) break;
        if (!["video", "image", "text"].includes(d.type)) continue;
        if (d.downloaded || isDownloadedCheck(d.id, d.bvid)
           || (d.type === "video" && isDownloadedCheck("0", d.bvid))) continue;
        const label = getDynDownloadLabel(d);
        if (!checkDownloadType(label, !!d.charge_only, true)) { skipped++; continue; }
        todo.push(d);
      }
      if (!data.has_more) { exhausted = true; break; }
      if (page >= 60) break;
      page++;
    }
  } catch (e) {
    alert("获取动态列表失败: " + e.message);
    btn.disabled = false; btn.textContent = "下载全部";
    return;
  }

  if (todo.length === 0) {
    alert(exhausted
      ? (skipped > 0
        ? `该UP的全部动态均已被下载或被下载设置跳过（${skipped} 条）✅`
        : "该UP的全部动态均已下载 ✅")
      : `未找到未下载的动态（已搜 ${page} 页，${skipped} 条被跳过）`);
    btn.disabled = false; btn.textContent = "下载全部";
    return;
  }
  if (!confirm(`将下载该UP共 ${todo.length} 条未下载动态${skipped > 0 ? `，${skipped} 条被跳过` : ""}，确认开始？`)) {
    btn.disabled = false; btn.textContent = "下载全部";
    return;
  }

  // 3) 逐条下载
  toggleDownloadPanel(true);
  startPolling();
  for (let i = 0; i < todo.length; i++) {
    const d = todo[i];
    btn.textContent = `下载中... (${i + 1}/${todo.length})`;
    await downloadDynamic(d, true);
    if (i < todo.length - 1) await sleep(1000);
  }
  btn.disabled = false; btn.textContent = "下载全部";
  rerenderCurrentTab();
  let doneMsg = `已触发下载 ${todo.length} 条动态`;
  if (skipped > 0) doneMsg += `，另跳过 ${skipped} 条被禁止下载的动态（可在下载设置里修改）`;
  alert(doneMsg);
}

// ==================== 已下载记录面板 ====================

let historyData = [];
let historyOnlyCurrent = false;
let historyPage = 1;
const HISTORY_PER_PAGE = 50;

async function openHistoryView() {
  historyPage = 1;
  const view = document.getElementById("historyView");
  const body = document.getElementById("historyBody");
  body.innerHTML = '<div class="empty"><div class="icon">📥</div><div>加载中…</div></div>';
  view.style.display = "flex";
  // 当前 UP主筛选条：有当前搜索用户才显示
  const filterEl = document.getElementById("historyFilter");
  const nameEl = document.getElementById("historyFilterName");
  if (currentData && currentData.user && currentData.user.uid) {
    filterEl.style.display = "flex";
    nameEl.textContent = currentData.user.name || currentData.user.uid;
  } else {
    filterEl.style.display = "none";
    historyOnlyCurrent = false;
    const cb = document.getElementById("historyOnlyCurrent");
    if (cb) cb.checked = false;
  }
  try {
    const resp = await fetch("/api/history");
    historyData = await resp.json();
  } catch (e) {
    historyData = [];
  }
  renderHistory();
}

function closeHistoryView() {
  document.getElementById("historyView").style.display = "none";
}

function toggleHistoryFilter(checked) {
  historyOnlyCurrent = checked;
  historyPage = 1;
  renderHistory();
}

function renderHistory() {
  const body = document.getElementById("historyBody");
  const uid = (historyOnlyCurrent && currentData && currentData.user) ? currentData.user.uid : null;
  let items = Array.isArray(historyData) ? historyData : [];
  if (uid) {
    items = items.filter(x => String(x.up_uid || "") === String(uid));
  }
  items.sort((a, b) => (b.time || "").localeCompare(a.time || ""));
  const total = items.length;
  const totalPages = Math.ceil(total / HISTORY_PER_PAGE);
  if (historyPage > totalPages) historyPage = totalPages || 1;
  const pageItems = items.slice((historyPage - 1) * HISTORY_PER_PAGE, historyPage * HISTORY_PER_PAGE);
  document.getElementById("historyStat").textContent =
    total > 0 ? `共 ${total} 条` : "暂无已下载记录";
  if (total === 0) {
    const tip = uid ? "当前 UP主还没有下载记录" : "还没有下载记录";
    body.innerHTML = `<div class="empty"><div class="icon">📭</div><div>${tip}</div></div>`;
    document.getElementById("historyPagination").innerHTML = "";
    return;
  }
  let html = "";
  pageItems.forEach(item => {
    html += historyItemHtml(item);
  });
  body.innerHTML = html;
  // 分页导航
  const pgEl = document.getElementById("historyPagination");
  if (pgEl) {
    let phtml = "";
    if (historyPage > 1) phtml += `<button class="page-btn" onclick="historyPage=1;renderHistory()">首页</button>
      <button class="page-btn" onclick="historyPage--;renderHistory()">上一页</button>`;
    phtml += `<span class="page-info">${historyPage}/${totalPages}</span>`;
    if (historyPage < totalPages) phtml += `<button class="page-btn" onclick="historyPage++;renderHistory()">下一页</button>
      <button class="page-btn" onclick="historyPage=${totalPages};renderHistory()">末页</button>`;
    pgEl.innerHTML = phtml;
  }
}

function historyItemHtml(item) {
  const title = String(item.title || "(无标题)")
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const dyid = String(item["data-dyid"] || "0");
  const bvid = String(item.bvid || "0");
  const safeUp = item.up_name
    ? String(item.up_name).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    : "";
  const upHtml = safeUp ? `<div class="history-up">UP：<b>${safeUp}</b></div>` : "";
  return `<div class="history-item">
    <div class="history-info">
      <div class="history-name" title="${title}">${title}</div>
      <div class="history-time">${item.time || ""}</div>
      ${upHtml}
    </div>
    <button class="history-del" onclick="removeHistoryItem('${dyid}', '${bvid}')" title="移除">×</button>
  </div>`;
}

async function removeHistoryItem(dyid, bvid) {
  try {
    await fetch("/api/history/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dyid, bvid })
    });
    unmarkDownloaded(dyid, bvid);
    historyData = (Array.isArray(historyData) ? historyData : []).filter(x =>
      !(x["data-dyid"] === dyid && dyid !== "0") &&
      !(x.bvid === bvid && bvid !== "0")
    );
    renderHistory();
  } catch (e) {
    alert("移除失败: " + e.message);
  }
}

async function confirmClearHistory() {
  // 勾选「仅看当前UP主」时，只清该 UP；否则清全部
  const uid = (historyOnlyCurrent && currentData && currentData.user && currentData.user.uid)
    ? String(currentData.user.uid) : "";
  const tip = uid
    ? `确定要清空「${currentData.user.name || currentData.user.uid}」的下载记录吗？\n（已下载的文件不会被删除，只是清除标记；其它 UP 的记录保留）`
    : "确定要清空所有下载记录吗？\n（已下载的文件不会被删除，只是清除标记）";
  if (!confirm(tip)) return;
  try {
    await fetch("/api/history/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(uid ? { uid } : {})
    });
    if (uid) {
      historyData = (Array.isArray(historyData) ? historyData : []).filter(x => String(x.up_uid) !== uid);
      downloadedItems.clear();
      (historyData || []).forEach(x => {
        if (x["data-dyid"] && x["data-dyid"] !== "0") downloadedItems.add("dy:" + x["data-dyid"]);
        if (x.bvid && x.bvid !== "0") downloadedItems.add("bv:" + x.bvid);
      });
    } else {
      historyData = [];
      downloadedItems.clear();
    }
    selectedVideos.clear();
    selectedDynamics.clear();
    historyPage = 1;
    renderHistory();
    if (currentData) rerenderCurrentTab();
  } catch (e) {
    alert("清除失败: " + e.message);
  }
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollStatus, 1500);
  pollStatus(); // 立即查一次
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollStatus() {
  try {
    const resp = await fetch("/api/status");
    const data = await resp.json();
    downloadTasks = data.tasks || {};
    // 检查是否有任务完成，只有真正完成（status=done）才标记已下载
    let needRefresh = false;
    for (const [taskId, task] of Object.entries(downloadTasks)) {
      const mapping = taskItemMap[taskId];
      if (!mapping) continue;
      if (task.status === "done" && !mapping.done) {
        // 下载成功，标记为已下载
        const mid = String(mapping.id);
        if (mapping.type === "video") {
          markDownloaded("0", mid);
          selectedVideos.delete(mid);
          // 直接回写当前列表项的 downloaded 标志，保证就地重渲染立即生效
          // （避免集合 key 与 d.id 因字符串/数字类型不一致导致 .has() 失效）
          if (currentData && currentData.videos) {
            const v = currentData.videos.find(x => String(x.bvid) === mid);
            if (v) v.downloaded = true;
          }
        } else {
          markDownloaded(mapping.id, mapping.bvid || "0");
          selectedDynamics.delete(mapping.id);
          if (currentData && currentData.dynamics) {
            const d = currentData.dynamics.find(x => String(x.id) === mid);
            if (d) d.downloaded = true;
          }
        }
        mapping.done = true;
        needRefresh = true;
      } else if (task.status === "error" && !mapping.done) {
        // 下载失败，不标记为已下载
        mapping.done = true;  // 标记为已处理（避免重复处理）
        needRefresh = true;
      }
    }
    renderDownloadPanel();
    // 有任务状态变化时刷新列表（按当前所在视图：我的视图用 switchSelfTab，UP主视图用 switchTab）
    if (needRefresh) {
      if (selfActive) switchSelfTab(selfStab);
      else if (currentData) rerenderCurrentTab();
    }
  } catch (e) {
    // 忽略网络错误
  }
}

function formatTimeAgo(ts) {
  if (!ts) return "";
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 60) return d + "秒前";
  if (d < 3600) return Math.floor(d / 60) + "分前";
  if (d < 86400) return Math.floor(d / 3600) + "小时前";
  return Math.floor(d / 86400) + "天前";
}

// 让下载任务的相对时间（X秒前）每秒自动刷新，无需重新拉取任务列表
function tickRelativeTimes() {
  document.querySelectorAll(".dl-time[data-ts]").forEach((el) => {
    const ts = Number(el.getAttribute("data-ts"));
    if (ts) el.textContent = formatTimeAgo(ts);
  });
}
if (typeof setInterval !== "undefined") setInterval(tickRelativeTimes, 1000);

function statusText(t) {
  const map = {
    queued: "排队中", downloading: "下载中", merging: "合并中",
    cancelling: "取消中", done: "已完成", error: "失败", cancelled: "已取消",
  };
  return map[t.status] || t.status;
}

function renderDownloadPanel() {
  const list = document.getElementById("downloadList");
  const statsEl = document.getElementById("dlStats");
  const ids = Object.keys(downloadTasks);

  // 统计：进行中 / 排队 / 成功 / 失败 / 已取消
  let run = 0, queued = 0, ok = 0, err = 0, cancelled = 0;
  for (const t of Object.values(downloadTasks)) {
    if (t.status === "queued") queued++;
    else if (t.status === "done") ok++;
    else if (t.status === "error") err++;
    else if (t.status === "cancelled") cancelled++;
    else run++;
  }

  if (ids.length === 0) {
    statsEl.innerHTML = `<span class="idle">空闲</span>`;
    list.innerHTML = `<div style="text-align:center;color:#7d8590;padding:20px;">暂无下载任务</div>`;
    return;
  }

  // 统计条：进行中 / 排队 / 成功 / 失败（含成功率）
  statsEl.innerHTML =
    `<span class="run">进行 ${run}</span>` +
    (queued ? `<span class="que">排队 ${queued}</span>` : "") +
    `<span class="ok">成功 ${ok}</span>` +
    `<span class="err">失败 ${err}</span>`;

  // 排序：活跃(排队/下载/合并/取消中)置顶，已完成沉底；同组按时间倒序(新在上)
  const rank = { queued: 0, retrying: 0, downloading: 1, merging: 2, cancelling: 3,
                 done: 4, error: 5, cancelled: 6 };
  ids.sort((a, b) => {
    const ta = downloadTasks[a], tb = downloadTasks[b];
    const ra = rank[ta.status] ?? 9, rb = rank[tb.status] ?? 9;
    if (ra !== rb) return ra - rb;
    return (tb.time || 0) - (ta.time || 0);
  });

  list.innerHTML = ids.map((id) => {
    const t = downloadTasks[id];
    const pct = t.progress || 0;
    const qBadge = t.quality ? `<span class="dl-qn">${escapeHtml(t.quality)}</span>` : "";
    const title = escapeHtml(t.title || t.message || statusText(t));
    const up = t.upname ? `<span class="dl-up">${escapeHtml(t.upname)}</span>` : "";
    const time = formatTimeAgo(t.time);

    // 失败/取消/重试中：显示错误码徽章 + 失败原因（鼠标悬停看原始异常）
    const errBadge = (["error", "retrying"].includes(t.status) && t.error_code)
      ? `<span class="dl-errcode" title="原始错误：${escapeHtml(t.error_detail || t.error_code)}">⚠ ${escapeHtml(t.error_code)}</span>`
      : "";
    let errLine = "";
    if (t.status === "error") {
      const reason = (t.message || "").replace(/^下载失败[:：]\s*/, "");
      errLine = `<div class="dl-err" title="原始错误：${escapeHtml(t.error_detail || "")}">${escapeHtml(reason)}</div>`;
    } else if (t.status === "retrying") {
      errLine = `<div class="dl-err retrying">${escapeHtml(t.message || "等待重试...")}</div>`;
    } else if (t.status === "cancelled") {
      errLine = `<div class="dl-err cancelled">已被用户取消</div>`;
    }

    let barHtml = "";
    if (["downloading", "merging", "cancelling", "retrying"].includes(t.status)) {
      barHtml = `<div class="dl-bar"><div class="dl-bar-fill ${t.status}" style="width:${pct}%"></div></div>`;
    }
    let action = "";
    if (["queued", "downloading", "merging", "cancelling", "retrying"].includes(t.status)) {
      action = `<button class="dl-btn cancel" title="取消" onclick="cancelTask('${id}')">✕</button>`;
    } else if (t.status === "error" || t.status === "cancelled") {
      // 失败 / 取消：提供重试
      action = `<button class="dl-btn retry" title="重试" onclick="retryTask('${id}')">↺</button>`;
    }
    // 下载成功的任务：不显示任何按钮（无需重试/删除）
    return `
      <div class="dl-item ${t.status}">
        <div class="dl-row">
          <span class="dl-msg" title="${title}">${title}</span>
          ${qBadge}
          ${errBadge}
          ${action}
        </div>
        <div class="dl-meta">${up}<span class="dl-st">${statusText(t)} · ${pct}%</span><span class="dl-time" data-ts="${t.time || ""}">${time}</span></div>
        ${errLine}
        ${barHtml}
      </div>`;
  }).join("");

  // 全部结束 → 停止轮询，并在状态从"有进行中"转为"全结束"时弹一次提示
  const active = ids.filter((id) =>
    !["done", "error", "cancelled"].includes(downloadTasks[id].status)
  );
  if (active.length === 0) {
    if (prevActiveCount > 0 && (ok + err + cancelled) > 0) {
      showToast(`下载完成 · 成功 ${ok} · 失败 ${err}` + (cancelled ? ` · 取消 ${cancelled}` : ""));
    }
    setTimeout(stopPolling, 3000);
  }
  prevActiveCount = active.length;
}

function showToast(msg) {
  let el = document.getElementById("dlToast");
  if (!el) {
    el = document.createElement("div");
    el.id = "dlToast";
    el.className = "dl-toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 4000);
}

async function cancelTask(taskId) {
  try {
    await fetch("/api/download/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    });
  } catch (e) { /* 忽略网络错误 */ }
}

async function retryTask(taskId) {
  try {
    const resp = await fetch("/api/download/retry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    });
    const d = await resp.json();
    if (d.ok && d.task_id) {
      // 继承旧的 任务→项目 映射，便于下载成功后标记
      taskItemMap[d.task_id] = taskItemMap[taskId] || { type: "?", id: "" };
      startPolling();
    } else if (d.reason === "active_exists") {
      // 已有同视频的进行中任务，跳过重发
      showToast(d.message || "该视频正在下载中，已跳过重复提交");
    }
  } catch (e) { /* 忽略网络错误 */ }
}

async function clearTasks(scope) {
  if (scope === "all" && !confirm("确定清空全部下载任务？（进行中的也会被移除）")) return;
  try {
    await fetch("/api/tasks/clear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope }),
    });
  } catch (e) { /* 忽略网络错误 */ }
  // 同步本地缓存：后端内存已清，但视图渲染用的是本地 downloadTasks，
  // 不在这里同步删除的话，列表不会变化（「清空已完成」点了没反应）。
  if (scope === "all") {
    downloadTasks = {};
    taskItemMap = {};
  } else {
    // 「清空已完成」只清真正下载成功的（done）；失败(error)/取消(cancelled) 保留，
    // 以便重试或查看，避免误删。要彻底清掉请点「清空全部」。
    for (const tid of Object.keys(downloadTasks)) {
      if (downloadTasks[tid].status === "done") {
        delete downloadTasks[tid];
        delete taskItemMap[tid];
      }
    }
  }
  renderDownloadPanel();
}

function toggleDownloadPanel(force) {
  const p = document.getElementById("downloadPanel");
  if (force === undefined) {
    p.classList.toggle("collapsed");
  } else if (force) {
    p.classList.remove("collapsed");
  } else {
    p.classList.add("collapsed");
  }
}


// ==================== 顶部导航「设置」下拉菜单 ====================
function toggleNavSettings(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById("navSettingsMenu");
  if (!menu) return;
  menu.style.display = (menu.style.display === "block") ? "none" : "block";
}
// 点击页面其它地方关闭「设置」菜单
document.addEventListener("click", function (e) {
  const menu = document.getElementById("navSettingsMenu");
  const wrap = document.getElementById("navSettings");
  if (menu && menu.style.display === "block" && wrap && !wrap.contains(e.target)) {
    menu.style.display = "none";
  }
});

// ==================== 下载画质（清晰度）选择 ====================
const QN_OPTIONS = [127, 120, 116, 112, 80, 64, 32, 16];
// 读取已保存的画质偏好（默认 1080P）
function getVideoQn() {
  const v = parseInt(localStorage.getItem("bili_qn") || "80", 10);
  return QN_OPTIONS.includes(v) ? v : 80;
}

function toggleHelp(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const visible = el.style.display === "block";
  el.style.display = visible ? "none" : "block";
}

// ==================== 下载设置弹窗（画质 + 保存位置 + 命名方式）====================
function openDlModal() {
  const qnSel = document.getElementById("qnSelectModal");
  fetch("/api/config").then(r => r.json()).then(cfg => {
    if (qnSel) qnSel.value = String(cfg.qn != null ? cfg.qn : getVideoQn());
    const inp = document.getElementById("downloadDirInput");
    if (inp) inp.value = cfg.download_dir || "";
    const ft = document.getElementById("folderTemplateInput");
    if (ft) ft.value = cfg.folder_template || "";
    const fl = document.getElementById("fileTemplateInput");
    if (fl) fl.value = cfg.file_template || "";
    const md = document.getElementById("maxDurationInput");
    if (md) md.value = cfg.max_duration || "0";
    const tc = document.getElementById("threadCountInput");
    if (tc) tc.value = cfg.download_threads || "3";
    const px = document.getElementById("proxyInput");
    if (px) px.value = cfg.proxy || "";
    const sl = document.getElementById("speedLimitInput");
    if (sl) sl.value = cfg.speed_limit || "0";
    // 下载类型复选框由 DYN_CATEGORIES 动态渲染（与 TAB 栏同步）；选中状态取自服务端配置，未配置则全选
    const dlTypes = cfg.download_types || [];
    renderDownloadTypeCheckboxes(dlTypes);
  }).catch(() => {});
  document.getElementById("dlModal").classList.add("show");
}
function closeDlModal() {
  document.getElementById("dlModal").classList.remove("show");
}
async function saveDlSettings() {
  const qnSel = document.getElementById("qnSelectModal");
  if (qnSel) localStorage.setItem("bili_qn", qnSel.value);
  const inp = document.getElementById("downloadDirInput");
  const ft = document.getElementById("folderTemplateInput");
  const fl = document.getElementById("fileTemplateInput");
  const md = document.getElementById("maxDurationInput");
  const tc = document.getElementById("threadCountInput");
  const ac = document.getElementById("typeCheckGroup");
  const payload = {};
  if (inp) payload.download_dir = inp.value.trim();
  if (ft) payload.folder_template = ft.value.trim();
  if (fl) payload.file_template = fl.value.trim();
  if (md) payload.max_duration = parseInt(md.value, 10) || 0;
  if (tc) payload.download_threads = Math.max(1, Math.min(8, parseInt(tc.value, 10) || 3));
  const px = document.getElementById("proxyInput");
  if (px) payload.proxy = px.value.trim();
  const sl = document.getElementById("speedLimitInput");
  if (sl) payload.speed_limit = Math.max(0, parseInt(sl.value, 10) || 0);
  // 画质 qn 写入全局 config，使"下载设置"成为唯一权威来源（手动/自动下载都读 config.qn）
  if (qnSel) payload.qn = getVideoQn();
  if (ac) {
    const types = [];
    ac.querySelectorAll("input[type=checkbox]:checked").forEach(cb => types.push(cb.value));
    payload.download_types = types;
      localStorage.setItem("bili_dl_types", types.join(","));
  }
  try {
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await resp.json();
    if (!d.ok) { alert("保存失败"); return; }
    closeDlModal();
  } catch (e) {
    alert("保存失败: " + e.message);
  }
}

// ==================== Cookie 管理 ====================

// ==================== 自动化下载弹窗 ====================
let autoUpList = []; // [{uid, name}]
let autoStatus = {}; // uid → {has_new, count}
let autoLogLastId = 0;   // 实时日志轮询游标
let autoLogTimer = null; // 轮询定时器
let autoRunning = false; // 自动化是否在运行

function openAutoModal() {
  document.getElementById("autoModal").classList.add("show");
  loadAutoData();
  startAutoLogPolling();
}
async function loadAutoData() {
  try {
    const r = await fetch("/api/config");
    const cfg = await r.json();
    autoUpList = cfg.auto_uids || [];
    // 同步起始日期
    const di = document.getElementById("autoCutoffDate");
    if (di) di.value = cfg.auto_cutoff_date || "";
    // 同步定时检查开关和间隔
    const toggle = document.getElementById("autoScheduleToggle");
    const sel = document.getElementById("autoIntervalSel");
    if (toggle) toggle.checked = cfg.auto_schedule_enabled || false;
    if (sel) {
      sel.disabled = !(cfg.auto_schedule_enabled || false);
      sel.value = String(cfg.auto_interval || 1800);
    }
    // 查询最新状态
    if (autoUpList.length) {
      const sr = await fetch("/api/auto/status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uids: autoUpList }),
      });
      autoStatus = await sr.json();
    } else {
      autoStatus = {};
    }
  } catch (e) {
    autoStatus = {};
  }
  renderAutoList();
}
async function addAutoUpManually() {
  const inp = document.getElementById("autoUidInput");
  if (!inp) return;
  const raw = inp.value.trim();
  if (!raw) return;
  const m = raw.match(/space\.bilibili\.com\/(\d+)/) || raw.match(/^(\d+)$/);
  if (!m) { alert("请输入有效的UID或UP主主页链接"); return; }
  const uid = m[1];
  if (autoUpList.some(u => String(u.uid) === uid)) { alert("该UP主已在列表中"); return; }
  inp.value = "";
  inp.placeholder = "正在获取UP主名...";
  let name = "";
  try {
    const resp = await fetch(`/api/search?query=${encodeURIComponent(uid)}&cookie=${encodeURIComponent(cookie)}`);
    const d = await resp.json();
    name = (d.user && d.user.name) || "";
  } catch (e) { /* 忽略 */ }
  autoUpList.push({ uid, name: name || "UID:" + uid, enabled: true });
  inp.placeholder = "输入UID或主页链接手动添加";
  // 即时持久化
  await persistAutoUids();
  loadAutoData();
}
function closeAutoModal() {
  document.getElementById("autoModal").classList.remove("show");
}

// ---- 自动化实时日志轮询 ----
function startAutoLogPolling() {
  stopAutoLogPolling();
  pollAutoLog();
  autoLogTimer = setInterval(pollAutoLog, 2000);
}
function stopAutoLogPolling() {
  if (autoLogTimer) { clearInterval(autoLogTimer); autoLogTimer = null; }
}
async function pollAutoLog() {
  try {
    const r = await fetch("/api/auto/log?after=" + autoLogLastId);
    const d = await r.json();
    if (typeof d.last_id === "number") autoLogLastId = d.last_id;
    autoRunning = !!d.running;
    updateAutoRunIndicator();
    updateAutoFloat();
    if (d.logs && d.logs.length) appendAutoLogs(d.logs);
  } catch (e) { /* 轮询失败忽略 */ }
}
function appendAutoLogs(logs) {
  const el = document.getElementById("autoLog");
  if (!el) return;
  const frag = document.createDocumentFragment();
  for (const l of logs) {
    const div = document.createElement("div");
    div.style.color = l.level === "error" ? "#ff7b72" : l.level === "warn" ? "#d29922" : "#9da7b3";
    div.textContent = `[${l.ts}] ${l.msg}`;
    frag.appendChild(div);
  }
  el.appendChild(frag);
  // 限制 DOM 数量，避免长时运行后卡顿
  while (el.childNodes.length > 300) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}
function updateAutoRunIndicator() {
  const pill = document.getElementById("autoRunPill");
  const txt = document.getElementById("autoRunText");
  if (pill) pill.classList.toggle("running", autoRunning);
  if (txt) txt.textContent = autoRunning ? "运行中" : "空闲";
}
function updateAutoFloat() {
  const f = document.getElementById("autoFloat");
  if (!f) return;
  f.style.display = autoRunning ? "flex" : "none";
  const dot = document.getElementById("autoFloatDot");
  if (dot) dot.classList.toggle("running", autoRunning);
}
function clearAutoLog() {
  const el = document.getElementById("autoLog");
  if (el) el.innerHTML = "";
}
function exportAutoLog() {
  // 直接下载服务端日志文件
  const a = document.createElement("a");
  a.href = "/api/logs/download";
  a.download = "";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
function renderAutoList() {
  const el = document.getElementById("autoUpList");
  const bar = document.getElementById("autoStatusBar");
  if (!el) return;
  if (!autoUpList.length) {
    el.innerHTML = `<div style="color:#7d8590;font-size:13px;text-align:center;padding:12px;">暂无监控 UP 主，请在上方输入 UID 添加</div>`;
    if (bar) bar.textContent = "";
    return;
  }
  let newTotal = 0;
  el.innerHTML = autoUpList.map(u => {
    const uid = String(u.uid);
    const enabled = u.enabled !== false;
    const st = autoStatus[uid] || {};
    if (st.has_new && enabled) newTotal += st.count;
    const badge = st.has_new && enabled ? `<span class="fav-up-badge new">${st.count}</span>` : "";
    const checkIcon = enabled
      ? `<span style="width:15px;height:15px;border-radius:50%;background:#fb7299;flex-shrink:0;"></span>`
      : `<span style="width:15px;height:15px;border-radius:50%;border:1.5px solid #30363d;flex-shrink:0;"></span>`;
    return `<div class="auto-up-item${enabled ? " checked" : ""}" onclick="toggleAutoUp('${escapeAttr(uid)}')" title="${escapeAttr('UID:' + uid)}">
      ${checkIcon}
      <span>${escapeHtml(u.name || "UID:" + u.uid)}</span>
      ${badge}
      <button class="au-remove" onclick="event.stopPropagation(); removeAutoUp('${escapeAttr(uid)}')" title="移出列表">&times;</button>
    </div>`;
  }).join("");
  if (bar) bar.textContent = newTotal ? `共 ${newTotal} 个未下载` : "";
}
async function persistAutoUids() {
  try {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_uids: autoUpList }),
    });
  } catch (e) { /* 忽略 */ }
}
async function saveAutoCutoffDate() {
  const input = document.getElementById("autoCutoffDate");
  const val = input ? input.value : "";
  try {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_cutoff_date: val }),
    });
  } catch (e) { /* 忽略 */ }
}
async function toggleAutoSchedule() {
  const toggle = document.getElementById("autoScheduleToggle");
  const sel = document.getElementById("autoIntervalSel");
  const enabled = toggle ? toggle.checked : false;
  if (sel) sel.disabled = !enabled;
  try {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_schedule_enabled: enabled }),
    });
  } catch (e) { /* 忽略 */ }
}
async function saveAutoInterval() {
  const sel = document.getElementById("autoIntervalSel");
  const val = sel ? parseInt(sel.value, 10) : 1800;
  try {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_interval: val }),
    });
  } catch (e) { /* 忽略 */ }
}
async function toggleAutoUp(uid) {
  const u = autoUpList.find(u => String(u.uid) === String(uid));
  if (u) {
    u.enabled = u.enabled === false ? true : false;
  }
  await persistAutoUids();
  renderAutoList();
}
async function removeAutoUp(uid) {
  const idx = autoUpList.findIndex(u => String(u.uid) === String(uid));
  if (idx >= 0) {
    autoUpList.splice(idx, 1);
    await persistAutoUids();
    renderAutoList();
  }
}
async function enableAllAutoUp() {
  const allEnabled = autoUpList.every(u => u.enabled !== false);
  autoUpList.forEach(u => u.enabled = !allEnabled);
  await persistAutoUids();
  renderAutoList();
}
async function manualAutoCheck() {
  const btn = document.getElementById("autoCheckBtn");
  if (btn) { btn.textContent = "检查中..."; btn.disabled = true; }
  try {
    await fetch("/api/auto/check", { method: "POST" });
    if (btn) { btn.textContent = "已触发"; setTimeout(() => { if (btn) { btn.textContent = "立即检查"; btn.disabled = false; } }, 2000); }
  } catch (e) {
    if (btn) { btn.textContent = "立即检查"; btn.disabled = false; }
  }
}

function openCookieModal() {
  document.getElementById("cookieInput").value = cookie;
  document.getElementById("cookieModal").classList.add("show");
}
function toggleCookieHelp() {
  const el = document.getElementById("cookieHelp");
  if (el) el.style.display = el.style.display === "none" ? "block" : "none";
}
async function verifyCookieModal() {
  const val = document.getElementById("cookieInput").value.trim();
  if (!val) { alert("请先填入 SESSDATA"); return; }
  const btn = document.getElementById("cookieVerifyBtn");
  if (btn) { btn.textContent = "验证中..."; btn.disabled = true; }
  try {
    const resp = await fetch("/api/check_cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie: val }),
    });
    const d = await resp.json();
    if (d.login) {
      if (btn) { btn.textContent = "✅ 有效"; btn.style.background = "#3fb950"; btn.style.border = "none"; }
    } else if (d.reason === "expired") {
      if (btn) { btn.textContent = "⚠ 已过期"; btn.style.background = "#f0883e"; btn.style.border = "none"; }
    } else {
      if (btn) { btn.textContent = "✗ 无效"; btn.style.background = "#f85149"; btn.style.border = "none"; }
    }
    setTimeout(() => { if (btn) { btn.textContent = "验证"; btn.style.background = ""; btn.style.border = ""; btn.disabled = false; } }, 3000);
  } catch (e) {
    if (btn) { btn.textContent = "验证"; btn.disabled = false; }
  }
}
function closeCookieModal() {
  document.getElementById("cookieModal").classList.remove("show");
}

async function saveCookie() {
  cookie = document.getElementById("cookieInput").value.trim();
  const remember = document.getElementById("rememberCookie").checked;
  updateCookieStatus();

  // 命名模板由「命名方式」弹窗单独管理，这里只处理 SESSDATA
  const payload = {};
  if (remember) {
    if (cookie) {
      // 保存到localStorage（前端持久化）
      localStorage.setItem("bilibili_sessdata", cookie);
      payload.sessdata = cookie;
    }
    // 勾选但不填 cookie：不动 SESSDATA
  } else {
    // 不记住则清除本地 + 服务端 SESSDATA
    localStorage.removeItem("bilibili_sessdata");
    payload.sessdata = "";
  }

  // 同时保存到服务器配置文件（跨设备持久化）
  fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {});

  closeCookieModal();
  // 保存后立即验证 SESSDATA 是否有效
  await verifyCookie();
  // 同步加载导航栏用户信息（头像+用户名），无需整页刷新
  if (cookie) {
    loadSelfChip();
  } else {
    // 清除了 cookie：隐藏导航栏用户头像
    const chip = document.getElementById("selfChip");
    if (chip) chip.style.display = "none";
  }
}

function updateCookieStatus() {
  const statusEl = document.getElementById("cookieStatus");
  const txt = document.getElementById("cookieStatusText");
  if (cookie) {
    // 已有 cookie 但尚未验证：先显示蓝色"验证中"，等 verifyCookie 给结论
    if (statusEl.dataset.state === "empty" || !statusEl.dataset.state) {
      statusEl.dataset.state = "checking";
      txt.textContent = "Cookie已设置·验证中";
    }
  } else {
    statusEl.dataset.state = "empty";
    txt.textContent = "未设置Cookie";
  }
}

async function verifyCookie() {
  const statusEl = document.getElementById("cookieStatus");
  const txt = document.getElementById("cookieStatusText");
  statusEl.dataset.state = "checking";
  txt.textContent = "验证中...";
  try {
    const resp = await fetch("/api/check_cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie }),
    });
    const d = await resp.json();
    if (d.login) {
      statusEl.dataset.state = "ok";
      txt.textContent = "✅ 已登录";
      // 前端 cookie 为空但服务端配置有效时，同步过来（保证下载也用登录态）
      if (!cookie && d.has_sessdata) {
        cookie = d.sessdata;
        localStorage.setItem("bilibili_sessdata", cookie);
      }
    } else if (d.reason === "empty") {
      statusEl.dataset.state = "empty";
      txt.textContent = "未设置Cookie";
    } else if (d.reason === "expired") {
      // 真正登录态失效（cookie 存在但 B站返回未登录）
      statusEl.dataset.state = "warn";
      txt.textContent = "⚠ SESSDATA已过期，请重新设置";
    } else if (d.reason === "blocked") {
      // 风控/网络受限，并非 cookie 过期 —— 不要误报"已过期"
      statusEl.dataset.state = "warn";
      txt.textContent = "⚠ 验证受限（风控/网络），稍后重试";
    } else {
      // reason === "error" 等请求异常：保留"已设置但未验证"，不误报过期
      statusEl.dataset.state = "checking";
      txt.textContent = "Cookie已设置（验证失败）";
    }
  } catch (e) {
    // 网络异常时退回"已设置但未验证"的提示，不误报过期
    if (cookie) {
      statusEl.dataset.state = "checking";
      txt.textContent = "Cookie已设置（验证失败）";
    } else {
      statusEl.dataset.state = "empty";
      txt.textContent = "未设置Cookie";
    }
  }
}

// ==================== 图片预览 ====================

function previewImage(url) {
  document.getElementById("imgPreviewImg").src = url;
  document.getElementById("imgPreview").classList.add("show");
}

// ==================== 工具函数 ====================

// 把文字里的 [表情名] 占位符替换成真正的表情图片（emoji_map 由后端从 rich_text_nodes 提取）
function renderTextWithEmoji(text, emojiMap) {
  let html = escapeHtml(text);
  if (!emojiMap || typeof emojiMap !== "object") return html;
  for (const [name, url] of Object.entries(emojiMap)) {
    if (!name || !url) continue;
    // 文字已被 escapeHtml，占位符里的特殊字符（如 &）也要转义后再匹配
    const escapedName = escapeHtml(name);
    const imgTag = `<img class="dy-emoji" src="${escapeHtml(url)}" alt="${escapedName}" title="${escapedName}" loading="lazy">`;
    html = html.split(escapedName).join(imgTag);
  }
  return html;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str || "";
  return div.innerHTML;
}

function escapeAttr(str) {
  // 强制转字符串，避免传入数字/对象等非字符串时 (str || "").replace 抛错
  const s = (str == null) ? "" : String(str);
  return s.replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

// 安全地把值编码为「单引号 HTML 属性内的 JS 字符串字面量」：
// 先用 JSON.stringify 得到双引号包裹的 JS 字符串（自动转义 " \ 等），
// 再把可能破坏 HTML 属性（单引号定界）的 ' 转成实体，
// 避免收藏夹名/标题含 '（如 Tom's 收藏）时点不动下载按钮。
function jsAttr(s) {
  return JSON.stringify(s == null ? "" : s).replace(/'/g, "&#39;");
}

function formatNum(n) {
  if (n >= 10000) return (n / 10000).toFixed(1) + "万";
  return n.toString();
}

// 视频时长格式化：支持秒数（数字或纯数字字符串）或已格式化的 "MM:SS" / "H:MM:SS"
function formatDuration(dur) {
  if (dur == null || dur === "") return "";
  // 已是 MM:SS / H:MM:SS 形式，直接用
  if (typeof dur === "string" && /^\d{1,2}(:\d{2}){1,2}$/.test(dur.trim())) return dur.trim();
  const sec = parseInt(dur, 10);
  if (isNaN(sec) || sec <= 0) return "";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (x) => String(x).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// ==================== 我的：登录用户 / 稍后再看 / 收藏夹 ====================
let selfInfo = { login: false, uid: 0, name: "", face: "", level: 0 };
let selfActive = false;
let selfStab = "watchlater";
let wlItems = [];
let favFolders = [];
const selfLoaded = { watchlater: false, favorites: false };
let wlPage = 1;          // 稍后再看当前页
let favPage = 1;         // 收藏夹当前页
const SELF_PER_PAGE = 12; // 我的视图每页条数（与 UP 页视觉同步）
let favFolderId = "";     // 当前选中的收藏夹子 TAB（"" = 全部收藏夹汇总）

// 打开页面时填充 navbar 头像（若已登录）——用户名 + 头像
async function loadSelfChip() {
  try {
    const resp = await fetch(`/api/self?cookie=${encodeURIComponent(cookie || "")}`);
    const data = await resp.json();
    selfInfo = data;
    if (data.login) {
      const chip = document.getElementById("selfChip");
      const av = document.getElementById("selfChipAvatar");
      const fb = document.getElementById("selfChipFallback");
      const nameEl = document.getElementById("selfChipName");
      if (av) { av.src = data.face || ""; av.style.display = data.face ? "block" : "none"; }
      if (fb) {
        fb.textContent = (data.name || "?").charAt(0);
        fb.style.display = data.face ? "none" : "flex";
      }
      if (nameEl) nameEl.textContent = data.name || "";
      if (chip) chip.style.display = "flex";
    }
  } catch (e) { /* 忽略 */ }
}

// 头像加载失败时回退为用户名首字符
function selfChipAvatarError() {
  const av = document.getElementById("selfChipAvatar");
  const fb = document.getElementById("selfChipFallback");
  if (av) av.style.display = "none";
  if (fb) {
    fb.textContent = (selfInfo && selfInfo.name) ? selfInfo.name.charAt(0) : "?";
    fb.style.display = "flex";
  }
}

function openSelfView() {
  if (!cookie) {
    alert("请先在右上角『Cookie设置』中填入 SESSDATA 登录后再查看『我的』");
    openCookieModal();
    return;
  }
  if (!selfInfo.login) {
    // cookie 可能刚设置，重新取一次
    loadSelfChip();
  }
  selfActive = true;
  const view = document.getElementById("selfView");
  if (view) view.style.display = "block";
  renderSelfHeader();
  switchSelfTab("watchlater");
}

function closeSelfView() {
  selfActive = false;
  const view = document.getElementById("selfView");
  if (view) view.style.display = "none";
}

function renderSelfHeader() {
  const av = document.getElementById("selfAvatar");
  const fb = document.getElementById("selfAvatarFallback");
  const nm = document.getElementById("selfName");
  const uid = document.getElementById("selfUid");
  if (selfInfo.face) {
    if (av) { av.src = selfInfo.face; av.style.display = "block"; }
    if (fb) fb.style.display = "none";
  } else {
    if (av) av.style.display = "none";
    if (fb) { fb.textContent = (selfInfo.name || "?").charAt(0); fb.style.display = "flex"; }
  }
  if (nm) nm.textContent = selfInfo.name || "未登录";
  if (uid) uid.textContent = selfInfo.uid ? ("UID: " + selfInfo.uid) : "";
}

function switchSelfTab(tab) {
  selfStab = tab;
  document.querySelectorAll("#selfTabs .self-tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.stab === tab);
  });
  const ft = document.getElementById("favFolderTabs");
  if (ft) ft.style.display = (tab === "favorites") ? "flex" : "none";
  if (tab === "watchlater") {
    if (!selfLoaded.watchlater) loadWatchLater();
    else renderWatchLater();
  } else if (tab === "favorites") {
    if (!selfLoaded.favorites) loadFavorites();
    else { renderFavFolderTabs(); renderFavorites(); }
  }
}

async function loadWatchLater() {
  const box = document.getElementById("selfContent");
  if (box) box.innerHTML = `<div class="empty"><div class="icon">⏳</div><div>正在加载稍后再看…</div></div>`;
  try {
    const resp = await fetch(`/api/watchlater?cookie=${encodeURIComponent(cookie || "")}`);
    const data = await resp.json();
    if (data.error) {
      if (box) box.innerHTML = `<div class="empty"><div class="icon">💥</div><div>${escapeHtml(data.error)}</div></div>`;
      return;
    }
    wlItems = data.items || [];
    selfLoaded.watchlater = true;
    wlPage = 1;
    renderWatchLater();
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty"><div class="icon">💥</div><div>加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderWatchLater() {
  const box = document.getElementById("selfContent");
  const wlCount = document.getElementById("wlCount");
  if (wlCount) wlCount.textContent = wlItems.length;
  if (!wlItems.length) {
    if (box) box.innerHTML = `<div class="empty"><div class="icon">📭</div><div>稍后再看列表为空</div></div>`;
    return;
  }
  const totalPages = Math.ceil(wlItems.length / SELF_PER_PAGE);
  if (wlPage > totalPages) wlPage = totalPages;
  if (wlPage < 1) wlPage = 1;
  const start = (wlPage - 1) * SELF_PER_PAGE;
  const pageItems = wlItems.slice(start, start + SELF_PER_PAGE);
  const grid = `<div class="self-grid">${pageItems.map(selfVideoCard).join("")}</div>`;
  const pagination = renderPagination(wlPage, totalPages, "loadWatchLaterPage");
  if (box) box.innerHTML = grid + pagination;
}

function loadWatchLaterPage(page) {
  wlPage = page;
  renderWatchLater();
  const sv = document.getElementById("selfView");
  if (sv) sv.scrollTop = 0;
}

async function loadFavorites() {
  const box = document.getElementById("selfContent");
  if (box) box.innerHTML = `<div class="empty"><div class="icon">⏳</div><div>正在加载收藏夹…</div></div>`;
  try {
    const resp = await fetch(`/api/favorites?cookie=${encodeURIComponent(cookie || "")}`);
    const data = await resp.json();
    if (data.error) {
      if (box) box.innerHTML = `<div class="empty"><div class="icon">💥</div><div>${escapeHtml(data.error)}</div></div>`;
      return;
    }
    favFolders = data.folders || [];
    selfLoaded.favorites = true;
    favPage = 1;
    // 去掉「全部」汇总 TAB 后，默认选中第一个收藏夹
    favFolderId = favFolders.length ? String(favFolders[0].id) : "";
    renderFavFolderTabs();
    renderFavorites();
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty"><div class="icon">💥</div><div>加载失败: ${escapeHtml(e.message)}</div></div>`;
  }
}

function renderFavorites() {
  const box = document.getElementById("selfContent");
  const favCount = document.getElementById("favCount");
  // 按当前选中的收藏夹子 TAB 过滤（favFolderId 为空表示没有任何收藏夹）
  let all = [];
  let f = null;
  if (favFolderId) {
    f = favFolders.find((x) => String(x.id) === String(favFolderId));
    if (f) all = (f.items || []).slice();
  }
  const totalVideos = favFolders.reduce((s, f) => s + (f.items ? f.items.length : 0), 0);
  if (favCount) favCount.textContent = totalVideos;
  if (!favFolders.length) {
    if (box) box.innerHTML = `<div class="empty"><div class="icon">📭</div><div>还没有创建收藏夹</div></div>`;
    return;
  }
  if (!all.length) {
    const msg = favFolderId ? "该收藏夹暂无内容" : "收藏夹暂无内容";
    if (box) box.innerHTML = `<div class="empty"><div class="icon">📭</div><div>${msg}</div></div>`;
    return;
  }
  const totalPages = Math.ceil(all.length / SELF_PER_PAGE);
  if (favPage > totalPages) favPage = totalPages;
  if (favPage < 1) favPage = 1;
  const start = (favPage - 1) * SELF_PER_PAGE;
  const pageItems = all.slice(start, start + SELF_PER_PAGE);
  const grid = `<div class="self-grid">${pageItems.map(v => selfVideoCard(v, f ? f.title : "")).join("")}</div>`;
  const pagination = renderPagination(favPage, totalPages, "loadFavoritesPage");
  if (box) box.innerHTML = grid + pagination;
}

// 渲染收藏夹子 TAB 栏（仅各收藏夹分类，不含「全部」汇总），点击切换对应收藏夹
function renderFavFolderTabs() {
  const bar = document.getElementById("favFolderTabs");
  if (!bar) return;
  let html = "";
  for (const f of favFolders) {
    const cnt = (f.items || []).length;
    const active = String(f.id) === String(favFolderId) ? "active" : "";
    html += `<div class="fav-sub-tab ${active}" onclick="switchFavFolder('${f.id}')" title="${escapeHtml(f.title)}">${escapeHtml(f.title)} <span>${cnt}</span></div>`;
  }
  bar.innerHTML = html;
}

function switchFavFolder(id) {
  favFolderId = id;
  favPage = 1;
  renderFavFolderTabs();
  renderFavorites();
}

function loadFavoritesPage(page) {
  favPage = page;
  renderFavorites();
  const sv = document.getElementById("selfView");
  if (sv) sv.scrollTop = 0;
}

// 渲染一个视频卡片（稍后再看 / 收藏夹 共用）
// favName：收藏夹名字（仅 favorites 来源用，用于二级子目录；watchlater 传空）
function selfVideoCard(v, favName = "") {
  const dur = formatDuration(v.duration);
  const durHtml = dur ? `<span class="sc-duration">${dur}</span>` : "";
  const cover = v.cover
    ? `<div class="sc-cover-wrap"><img class="sc-cover" src="${v.cover}" loading="lazy" onerror="this.style.display='none'">${durHtml}</div>`
    : "";
  const owner = v.owner_name ? `<div class="sc-owner">UP: ${escapeHtml(v.owner_name)}</div>` : "";
  const direct = v.bvid
    ? `<a class="btn-direct" href="https://www.bilibili.com/video/${encodeURIComponent(v.bvid)}" target="_blank" rel="noopener">直达</a>`
    : "";
  const isDl = v.bvid && (v.downloaded || isDownloadedCheck("0", v.bvid));
  const dl = v.bvid
    ? `<button class="btn-download${isDl ? " downloaded" : ""}" ${isDl ? "disabled" : ""} onclick="downloadVideo('${v.bvid}', '${escapeAttr(v.title)}', '${escapeAttr(v.owner_name || "未知UP主")}', false, null, '', '${selfStab}', '${escapeAttr(favName)}')">${isDl ? "已下载" : "下载"}</button>`
    : "";
  return `<div class="self-card">
    ${cover}
    <div class="sc-body">
      <div class="sc-title">${escapeHtml(v.title)}</div>
      ${owner}
      <div class="dy-actions">${direct}${dl}</div>
    </div>
  </div>`;
}

// ==================== 启动初始化 ====================
initApp();
