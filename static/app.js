/* =====================================================================
   FBA 发货文件管理 — 前端单文件（Vue3 CDN，无构建）
   区块索引：
     [0] 工具：api() / toast / 全局 store / 格式化
     [1] 通用组件：modal
     [2] 页面：批次列表 page-batches
     [3] 页面：批次详情 page-batch-detail
     [4] 页面：产品库 page-products
     [5] 页面：工厂管理 page-factories
     [6] 页面：主体与品牌 page-companies
     [7] 页面：货代 & 模板 page-forwarders（含三步向导）
     [8] 页面：规则配置 page-rules
     [9] 页面：生成记录 page-docs
     [10] 根应用 + hash 路由
   ===================================================================== */
'use strict';
const { createApp, reactive } = Vue;

/* ============ [0] 工具 ============ */

/** fetch 封装：JSON / FormData 自适应；非 2xx 读 detail 抛 Error */
async function api(path, opts = {}) {
  const { method = 'GET', body } = opts;
  const init = { method, headers: {} };
  if (body instanceof FormData) {
    init.body = body;                       // multipart：浏览器自带 boundary
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch('/api' + path, init);
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try {
      const j = await res.json();
      if (j && j.detail !== undefined) {
        detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
      } else if (j) detail = JSON.stringify(j);
    } catch (e) { /* 非 JSON 响应 */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get('Content-Type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

/** POST 后直接下载返回的文件（试生成用，GET 类下载一律用 <a href>） */
async function postDownload(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.status + ' ' + res.statusText;
    try { const j = await res.json(); if (j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (e) {}
    throw new Error(detail);
  }
  const blob = await res.blob();
  let fname = 'download.xlsx';
  const cd = res.headers.get('Content-Disposition') || '';
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(cd);
  if (m) { try { fname = decodeURIComponent(m[1]); } catch (e) { fname = m[1]; } }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  return fname;
}

/* toast：成功绿 / 失败红，2.5s 自动消失 */
const toasts = reactive([]);
let _toastId = 0;
function toast(msg, type = 'ok') {
  const t = { id: ++_toastId, msg: String(msg), type };
  toasts.push(t);
  setTimeout(() => {
    const i = toasts.indexOf(t);
    if (i >= 0) toasts.splice(i, 1);
  }, 2500);
}

/* 全局缓存：下拉框等共享数据 */
const store = reactive({
  factories: [], companies: [], brands: [], forwarders: [],
  templates: [], batches: [], fields: null,
});
async function ensure(name, force = false) {
  if (!force && store[name] && store[name].length) return store[name];
  try {
    store[name] = (await api('/' + name)) || [];
  } catch (e) { toast('加载 ' + name + ' 失败：' + e.message, 'err'); }
  return store[name];
}

/* 格式化 / 标签 */
function fmtTime(s) { return s ? String(s).replace('T', ' ').slice(0, 16) : '-'; }
function num(v) { return (v === null || v === undefined || v === '') ? '-' : v; }
function money(v) { return (v === null || v === undefined || v === '' || isNaN(v)) ? '-' : Number(v).toFixed(2); }
function badgeCls(s) {
  return { '已同步': 'b-blue', '已核对': 'b-cyan', '已生成': 'b-green', '待投保': 'b-orange', '已投保': 'b-orange', '完成': 'b-dark' }[s] || 'grey';
}
function granLabel(g) {
  return { shipment: '每货件一份', batch: '每批次一份', row_per_shipment: '每货件一行' }[g] || g || '-';
}
const DOC_TYPES = ['托书', '报关资料', '投保单', '采购合同', '9810', '装箱单', '其他'];
// 店铺主体「默认文件清单」可选项
const COMPANY_DOC_TYPES = ['托书', '报关资料', '投保单', '外箱标', '货代标', '采购合同', '装箱单', '发票', '9810', '其他'];

/* 校验报告 fix_hint → 页面跳转 */
function fixHref(e) {
  const m = /^([\w-]+):(\d+)$/.exec(e && e.fix_hint || '');
  if (!m) return '';
  const map = {
    products: '#products/' + m[2],
    brands: '#companies/brands',
    companies: '#companies',
    factories: '#factories',
    forwarders: '#forwarders',
    batches: '#batches/' + m[2],
  };
  return map[m[1]] || '';
}

/* ============ [1] 通用组件 modal ============ */
const Modal = {
  props: ['title', 'size'],
  emits: ['close'],
  template: `
  <div class="overlay" @mousedown.self="$emit('close')">
    <div class="modal" :class="size">
      <div class="modal-head"><h3>{{title}}</h3>
        <button class="icon-btn" type="button" title="关闭" aria-label="关闭" @click="$emit('close')">✕</button></div>
      <div class="modal-body"><slot></slot></div>
      <div class="modal-foot" v-if="$slots.foot"><slot name="foot"></slot></div>
    </div>
  </div>`,
};

/* ============ [2] 批次列表 ============ */
const PageBatches = {
  template: '#tpl-batches',
  data() {
    return {
      batches: [], loading: true,
      // 采购计划三步向导
      syncOpen: false, syncStep: 1,
      plans: [], plansLoading: false, planPage: 1, planPageSize: 20, planTotal: 0,
      planStatus: '全部', statusCounts: {},
      pickedPlan: null,
      candidates: [], candLoading: false, autoMatched: false, pickedCand: null,
      preview: null, previewLoading: false,
      importing: false,
      // 离线 Excel 导入
      excelOpen: false, excelBusy: false,
    };
  },
  async mounted() {
    try { this.batches = (await api('/batches')) || []; }
    catch (e) { toast('加载批次失败：' + e.message, 'err'); }
    this.batches.sort((a, b) => (b.id || 0) - (a.id || 0));
    this.loading = false;
  },
  methods: {
    scoreCls(s) { const n = Number(s); return n >= 80 ? 'b-green' : (n >= 50 ? 'b-orange' : 'grey'); },
    planStatusCls(label) { return label === '已采购' ? 'b-green' : (label === '待采购' ? 'b-blue' : (label === '已驳回' ? 'red' : 'b-orange')); },

    /* ---- 向导开关 / 导航 ---- */
    openSync() {
      this.syncOpen = true; this.syncStep = 1;
      this.pickedPlan = null; this.pickedCand = null;
      this.candidates = []; this.preview = null; this.autoMatched = false;
      this.loadPlans(1);
    },
    closeSync() { if (this.importing) return; this.syncOpen = false; },
    gotoStep(n) {
      // 仅允许回退到已完成步骤（不可跳到未就绪的后续步）
      if (this.importing) return;
      if (n < this.syncStep) this.syncStep = n;
    },

    /* ---- 步骤1：采购计划 ---- */
    setPlanStatus(s) { this.planStatus = s; this.loadPlans(1); },
    async loadPlans(page) {
      this.plansLoading = true; this.planPage = page;
      try {
        const st = this.planStatus && this.planStatus !== '全部'
          ? '&status=' + encodeURIComponent(this.planStatus) : '';
        const r = await api('/sync/purchase-plans?page=' + page + st) || {};
        this.plans = r.plans || [];
        this.planTotal = r.total_size || this.plans.length;
        this.statusCounts = r.status_counts || {};
        if (r.page) this.planPage = r.page;
      } catch (e) { toast('拉取采购计划失败：' + e.message, 'err'); this.plans = []; }
      this.plansLoading = false;
    },
    pickPlan(p) {
      this.pickedPlan = p;
      // 切换计划后清空后续步骤状态
      this.pickedCand = null; this.candidates = []; this.preview = null; this.autoMatched = false;
    },
    async toStep2() {
      if (!this.pickedPlan) return;
      this.syncStep = 2;
      await this.loadCandidates(this.pickedPlan.plan_group_no);
    },
    async loadCandidates(planGroupNo) {
      this.candLoading = true; this.candidates = []; this.pickedCand = null; this.autoMatched = false;
      try {
        const r = await api('/sync/purchase-plans/' + encodeURIComponent(planGroupNo) + '/candidates') || {};
        this.candidates = r.candidates || [];
        if (r.auto_inbound_plan_id) {
          const auto = this.candidates.find(c => c.inbound_plan_id === r.auto_inbound_plan_id);
          if (auto) { this.pickedCand = auto; this.autoMatched = true; }
        }
      } catch (e) { toast('匹配入库计划失败：' + e.message, 'err'); this.candidates = []; }
      this.candLoading = false;
    },
    pickCand(c) { this.pickedCand = c; this.autoMatched = false; this.preview = null; },

    /* ---- 步骤3：预览 ---- */
    async toStep3() {
      if (!this.pickedPlan || !this.pickedCand) return;
      this.syncStep = 3;
      await this.loadPreview();
    },
    async loadPreview() {
      this.previewLoading = true; this.preview = null;
      try {
        this.preview = await api('/sync/purchase-plans/preview', {
          method: 'POST',
          body: { plan_group_no: this.pickedPlan.plan_group_no, inbound_plan_id: this.pickedCand.inbound_plan_id },
        });
      } catch (e) { toast('生成预览失败：' + e.message, 'err'); }
      this.previewLoading = false;
    },
    async doImport() {
      if (!this.pickedPlan || !this.pickedCand) return;
      this.importing = true;
      try {
        const r = await api('/sync/purchase-plans/import', {
          method: 'POST',
          body: { plan_group_no: this.pickedPlan.plan_group_no, inbound_plan_id: this.pickedCand.inbound_plan_id },
        });
        toast('导入成功，正在打开批次详情');
        try { this.batches = (await api('/batches')) || []; this.batches.sort((a, b) => (b.id || 0) - (a.id || 0)); } catch (e) {}
        this.syncOpen = false;
        const bid = r && (r.batch_id || r.id);
        if (bid) location.hash = '#batches/' + bid;
      } catch (e) { toast('导入失败：' + e.message, 'err'); }
      this.importing = false;
    },

    /* ---- 离线 Excel 导入 ---- */
    openExcel() { this.excelOpen = true; },
    async excelImport(ev) {
      const f = ev.target && ev.target.files && ev.target.files[0];
      if (!f) return;
      this.excelBusy = true;
      try {
        const fd = new FormData(); fd.append('file', f);
        const r = await api('/sync/import-excel', { method: 'POST', body: fd });
        toast('导入成功，正在打开批次详情');
        try { this.batches = (await api('/batches')) || []; this.batches.sort((a, b) => (b.id || 0) - (a.id || 0)); } catch (e) {}
        this.excelOpen = false;
        const bid = r && (r.batch_id || r.id);
        if (bid) location.hash = '#batches/' + bid;
      } catch (e) { toast('导入失败：' + e.message, 'err'); }
      finally { this.excelBusy = false; if (ev.target) ev.target.value = ''; }
    },

    async del(b) {
      if (!confirm('删除批次「' + (b.name || b.inbound_plan_id) + '」？\n将删除批次及全部生成文件（含 output 归档目录），不可恢复。')) return;
      try {
        const r = await api('/batches/' + b.id, { method: 'DELETE' });
        this.batches = this.batches.filter(x => x.id !== b.id);
        toast('已删除批次' + (r && r.files_removed ? '，清理生成文件 ' + r.files_removed + ' 个' : ''));
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
  },
};

/* ============ [2.5] 采购确认 ============ */
/* 供应商映射（PURCHASE_ORDER_API.md）：链条系→杭州朗格链条 / 其他→嘉欣 */
const CHAIN_BRANDS = ['zentop', 'byane', 'razedg'];
function isChainBrand(name) {
  const s = String(name || '').toLowerCase();
  return CHAIN_BRANDS.some(b => s.includes(b));
}

const PagePurchase = {
  template: '#tpl-purchase',
  data() {
    return {
      plans: [], plansLoading: false, planPage: 1, planPageSize: 20, planTotal: 0,
      planStatus: '待采购', statusCounts: {},  // 默认只看待采购（已审批待生成采购单）
      confirms: {}, confLoading: {},          // plan_group_no → 确认状态 / 查询中
      picked: null, rows: [],
      confirming: false,
      // 生成采购单
      orderOpen: false, orderLoading: false, orderCreating: false,
      orderPreview: null, orderRaw: null, orderAction: 2, orderForce: false,
      // 下单 / 到货 / 作废
      submitting: false, cancelling: false,
      arrivalOpen: false, arriving: false, arrivalRows: [], arrivalType: 0,
      // 采购流程状态链（从待采购起步，待审核归赛狐采购员审批，本系统不管）
      flowSteps: ['待采购', '待下单', '待到货', '已完成'],
    };
  },
  computed: {
    curConf() { return this.picked ? this.confirms[this.picked.plan_group_no] : null; },
    /* 有效状态：有采购单时以赛狐采购单实时状态为准；否则起点=待采购 */
    effStatus() {
      const c = this.curConf;
      if (c && c.po_status_label) {
        const pl = c.po_status_label;
        if (['待下单', '待到货', '已完成'].includes(pl)) return pl;
        if (pl === '已取消') return '待采购';
        return '待下单';                       // 草稿 / 待审核
      }
      if (c && ['待下单', '待到货', '已完成'].includes(c.status)) return c.status;
      return '待采购';                          // 待采购计划，尚未生成采购单
    },
    firstItem() { return (this.picked && (this.picked.items || [])[0]) || {}; },
    shopName() { return this.firstItem.shop_name || ''; },
    brandName() { return this.firstItem.brand_name || ''; },
    siteName() { return this.firstItem.site_name || '美国'; },
    supplierName() {
      if (this.firstItem.supplier_name) return this.firstItem.supplier_name;
      return isChainBrand(this.brandName || this.shopName) ? '杭州朗格链条有限公司' : '安庆市嘉欣医疗用品科技有限公司';
    },
  },
  async mounted() { await this.loadPlans(1); },
  methods: {
    planStatusCls(label) { return label === '已采购' ? 'b-green' : (label === '待采购' ? 'b-blue' : (label === '已驳回' ? 'red' : 'b-orange')); },
    confState(p) { return this.confirms[p.plan_group_no] || null; },
    confCls(status) {
      return {
        '已完成': 'b-green', '待到货': 'b-orange', '待下单': 'b-blue',
        '待采购': 'b-blue', '待审核': 'grey',
      }[status] || 'grey';
    },
    /* 当前计划在流程链里的索引（-1=还没确认，即待审核） */
    flowIdx(status) { return this.flowSteps.indexOf(status); },
    setPlanStatus(s) { this.planStatus = s; this.loadPlans(1); },
    async loadPlans(page) {
      this.plansLoading = true; this.planPage = page;
      try {
        const st = this.planStatus && this.planStatus !== '全部'
          ? '&status=' + encodeURIComponent(this.planStatus) : '';
        const r = await api('/sync/purchase-plans?page=' + page + st) || {};
        this.plans = r.plans || [];
        this.planTotal = r.total_size || this.plans.length;
        this.statusCounts = r.status_counts || {};
        if (r.page) this.planPage = r.page;
      } catch (e) { toast('拉取采购计划失败：' + e.message, 'err'); this.plans = []; }
      this.plansLoading = false;
    },
    /* 选中时再查确认状态，避免一次太多请求 */
    async loadConfirm(planGroupNo, force) {
      if (!force && this.confirms[planGroupNo]) return this.confirms[planGroupNo];
      this.confLoading[planGroupNo] = true;
      try {
        const r = await api('/purchase/confirm/' + encodeURIComponent(planGroupNo)) || {};
        this.confirms[planGroupNo] = {
          status: r.status || '待审核', purchase_no: r.purchase_no || '',
          po_status_label: r.po_status_label || '',
        };
      } catch (e) {
        // 查不到视为待审核，不打扰用户
        this.confirms[planGroupNo] = { status: '待审核', purchase_no: '', po_status_label: '' };
      }
      this.confLoading[planGroupNo] = false;
      return this.confirms[planGroupNo];
    },
    pick(p) {
      this.picked = p;
      // 核对明细：数量=plan_num，箱数=carton_num
      this.rows = (p.items || []).map(it => ({
        sku: it.sku, commodity_name: it.commodity_name, main_image: it.main_image,
        num: it.plan_num ?? 0, box_count: it.carton_num ?? 0,
      }));
      this.loadConfirm(p.plan_group_no);
    },
    close() { this.picked = null; this.rows = []; },
    async doConfirm() {
      if (!this.picked) return;
      this.confirming = true;
      try {
        await api('/purchase/confirm', {
          method: 'POST',
          body: {
            plan_group_no: this.picked.plan_group_no,
            items: this.rows.map(r => ({ sku: r.sku, commodity_name: r.commodity_name, num: r.num, box_count: r.box_count })),
            confirmed_by: '',
          },
        });
        this.confirms[this.picked.plan_group_no] = { status: '待采购', purchase_no: '', po_status_label: '' };
        toast('工厂确认完成 → 待采购');
      } catch (e) { toast('确认失败：' + e.message, 'err'); }
      this.confirming = false;
    },
    /* 生成采购单：先 dry_run 预览 */
    async openOrder() {
      if (!this.picked) return;
      this.orderOpen = true; this.orderAction = 2; this.orderForce = false;
      this.orderPreview = null; this.orderRaw = null; this.orderLoading = true;
      try {
        const r = await api('/purchase/create-order', {
          method: 'POST',
          body: { plan_group_no: this.picked.plan_group_no, action: 0, dry_run: true },
        }) || {};
        // 预览 body：兼容后端直接返回 body 或包一层 {body}
        this.orderRaw = r.body || r;
        this.orderPreview = r.body || r;
      } catch (e) { toast('生成预览失败：' + e.message, 'err'); this.orderOpen = false; }
      this.orderLoading = false;
    },
    async doCreate() {
      if (!this.picked) return;
      this.orderCreating = true;
      try {
        const r = await api('/purchase/create-order', {
          method: 'POST',
          body: { plan_group_no: this.picked.plan_group_no, action: this.orderAction, dry_run: false, force: this.orderForce, auto_arrival: true },
        }) || {};
        const pno = r.purchase_no || (r.body && r.body.purchase_no) || '';
        const st = r.status || (this.orderAction === 2 ? '待到货' : '待下单');
        this.confirms[this.picked.plan_group_no] = { status: st, purchase_no: pno, po_status_label: '' };
        if (r.arrival_error) {
          toast('采购单已生成下单' + (pno ? '：' + pno : '') + '，但自动到货失败：' + r.arrival_error + '（停在待到货，可手动到货）', 'err');
        } else {
          toast('采购单已生成·下单·到货' + (pno ? '：' + pno : '') + ' → ' + st);
        }
        this.orderOpen = false;
        await this.loadConfirm(this.picked.plan_group_no, true);
      } catch (e) { toast('生成采购单失败：' + e.message, 'err'); }
      this.orderCreating = false;
    },
    /* 下单（赛狐写）：待下单 → 待到货 */
    async doSubmitOrder() {
      if (!this.picked || !this.curConf || !this.curConf.purchase_no) return;
      if (!confirm('将在赛狐对采购单「' + this.curConf.purchase_no + '」执行下单，确认？')) return;
      this.submitting = true;
      try {
        await api('/purchase/submit-order', {
          method: 'POST', body: { plan_group_no: this.picked.plan_group_no },
        });
        toast('已下单 → 待到货');
        await this.loadConfirm(this.picked.plan_group_no, true);
      } catch (e) { toast('下单失败：' + e.message, 'err'); }
      this.submitting = false;
    },
    /* 到货：填每个 SKU 的良品 / 次品数 */
    openArrival() {
      if (!this.picked) return;
      this.arrivalType = 0;
      this.arrivalRows = (this.rows.length ? this.rows : (this.picked.items || []).map(it => ({
        sku: it.sku, commodity_name: it.commodity_name, main_image: it.main_image, num: it.plan_num ?? 0,
      }))).map(r => ({
        sku: r.sku, commodity_name: r.commodity_name, main_image: r.main_image,
        goods: r.num ?? 0, defective: 0,   // 默认良品=数量，次品=0
      }));
      this.arrivalOpen = true;
    },
    async doArrival() {
      if (!this.picked || !this.curConf || !this.curConf.purchase_no) return;
      if (!confirm('将在赛狐对采购单「' + this.curConf.purchase_no + '」登记到货，确认？')) return;
      this.arriving = true;
      try {
        await api('/purchase/arrival', {
          method: 'POST',
          body: {
            plan_group_no: this.picked.plan_group_no,
            arrival_type: this.arrivalType,
            items: this.arrivalRows.map(r => ({
              sku: r.sku, goods: r.goods ?? 0, defective: r.defective ?? 0,
            })),
          },
        });
        toast('到货登记完成 → 已完成');
        this.arrivalOpen = false;
        await this.loadConfirm(this.picked.plan_group_no, true);
      } catch (e) { toast('到货登记失败：' + e.message, 'err'); }
      this.arriving = false;
    },
    /* 删除本地采购确认记录（不碰赛狐 PO） */
    async doDeleteLocal() {
      if (!this.picked) return;
      const pno = this.curConf && this.curConf.purchase_no;
      if (!confirm('仅删除本系统的采购确认记录' + (pno ? '（不影响赛狐采购单 ' + pno + '）' : '') + '，确认？')) return;
      try {
        await api('/purchase/confirm/' + encodeURIComponent(this.picked.plan_group_no), { method: 'DELETE' });
        delete this.confirms[this.picked.plan_group_no];
        toast('本地记录已删除');
        this.close();
        await this.loadPlans(this.planPage);
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    /* 作废采购单（赛狐写）→ 回到待采购，可重新生成 */
    async doCancel() {
      if (!this.picked || !this.curConf || !this.curConf.purchase_no) return;
      if (!confirm('将作废赛狐采购单「' + this.curConf.purchase_no + '」并回到「待采购」，确认？\n（已完成的单赛狐通常拒绝作废）')) return;
      this.cancelling = true;
      try {
        const r = await api('/purchase/cancel-order', {
          method: 'POST', body: { plan_group_no: this.picked.plan_group_no },
        }) || {};
        if (r.failed && r.failed.length) {
          toast('部分作废失败：' + r.failed.map(f => f.purchase_no).join('、') + '（已完成的单不能作废）', 'err');
        } else {
          toast('已作废 → 待采购，可重新生成');
        }
        await this.loadConfirm(this.picked.plan_group_no, true);
      } catch (e) { toast('作废失败：' + e.message, 'err'); }
      this.cancelling = false;
    },
  },
};

/* ============ [3] 批次详情 ============ */
const TOTAL_LABELS = {
  shipment_count: '货件数', shipments: '货件数',
  carton_num: '总箱数', cartons: '总箱数', total_cartons: '总箱数',
  qty: '总数量', total_qty: '总数量',
  value: '总金额', total_value: '总金额', amount: '总金额',
  weight: '总毛重(kg)', total_weight: '总毛重(kg)',
  volume: '总体积(m³)', total_volume: '总体积(m³)',
  sku_count: 'SKU数',
};

const PageBatchDetail = {
  template: '#tpl-batch-detail',
  props: ['arg'],
  data() {
    return {
      batch: null, loading: true,
      report: null, validating: false,
      detailTab: 'generate', shipOpen: {},
      boxOpen: {},
      templates: [], selectedTplIds: [],
      generating: false, genErrors: [],
      resyncBusy: false, offlineOpen: false, offlineBusy: false,
      previewBusyId: null, previewData: null, previewTpl: null, previewTab: 0,
      fieldMap: null,
      histOpen: {}, previewDoc: null,
      _vt: null,
    };
  },
  computed: {
    isOffline() {
      return String((this.batch && this.batch.inbound_plan_id) || '').startsWith('offline-');
    },
    /* 文件区收纳：按 (模板, 货件) 分组，只平铺最新一份，旧版本折叠为历史 */
    docGroups() {
      const m = new Map();
      for (const d of ((this.batch && this.batch.docs) || [])) {
        const k = (d.template_id ?? ('t:' + (d.doc_type || ''))) + '|' + (d.shipment_id ?? 0);
        if (!m.has(k)) m.set(k, []);
        m.get(k).push(d);
      }
      const out = [];
      for (const [key, list] of m) {
        list.sort((a, b) => ((b.id ?? b.doc_id) || 0) - ((a.id ?? a.doc_id) || 0));
        out.push({ key, latest: list[0], history: list.slice(1) });
      }
      out.sort((a, b) => (((b.latest.id ?? b.latest.doc_id) || 0) - ((a.latest.id ?? a.latest.doc_id) || 0)));
      return out;
    },
    reportErrors() { return ((this.report && this.report.errors) || []).filter(e => e.level === 'error'); },
    reportWarns() { return ((this.report && this.report.errors) || []).filter(e => e.level !== 'error'); },
    totalChips() {
      const t = (this.batch && this.batch.totals) || {};
      const out = {};
      for (const [k, v] of Object.entries(t)) {
        if (v === null || v === undefined) continue;
        out[TOTAL_LABELS[k] || k] = (typeof v === 'number' && !Number.isInteger(v)) ? v.toFixed(2) : v;
      }
      return out;
    },
    templateGroups() {
      const fids = new Set(((this.batch && this.batch.shipments) || []).map(s => s.forwarder_id).filter(Boolean));
      const g1 = [], g2 = [], g3 = [];
      for (const t of this.templates) {
        if (t.owner_type === 'internal') g2.push(t);
        else if (fids.has(t.forwarder_id)) g1.push(t);
        else g3.push(t);
      }
      return [
        { label: '本批货代模板', list: g1 },
        { label: '内部模板', list: g2 },
        { label: '其他模板', list: g3 },
      ];
    },
  },
  async mounted() {
    ensure('factories');
    await this.reload(true);
    await this.runValidate();
    // 首次进入若校验未过，落在能看到校验的「概览」tab
    if (this.reportErrors.length) this.detailTab = 'overview';
  },
  methods: {
    fixHref,
    toggleShip(s) { this.shipOpen[s.id] = !this.shipOpen[s.id]; },
    expandAllShips(open) {
      for (const s of (this.batch && this.batch.shipments) || []) this.shipOpen[s.id] = !!open;
    },
    async reload(first) {
      try {
        this.batch = await api('/batches/' + this.arg + '/full');
      } catch (e) { toast('加载批次失败：' + e.message, 'err'); this.batch = null; }
      if (first) {
        try {
          this.templates = (await api('/templates')) || [];
          const suggested = this.batch && this.batch.suggested_template_ids;
          if (suggested !== undefined && suggested !== null) {
            // 后端已算好（店铺清单 ∩ 本批货代+内部的启用模板）
            const sset = new Set(suggested);
            this.selectedTplIds = this.templates.filter(t => sset.has(t.id)).map(t => t.id);
          } else {
            // 兜底：启用 && (内部模板 || 本批货代)
            const fids = new Set(((this.batch && this.batch.shipments) || []).map(s => s.forwarder_id).filter(Boolean));
            this.selectedTplIds = this.templates
              .filter(t => t.active && (t.owner_type === 'internal' || fids.has(t.forwarder_id)))
              .map(t => t.id);
          }
        } catch (e) { toast('加载模板失败：' + e.message, 'err'); }
      }
      this.loading = false;
    },
    async runValidate() {
      if (!this.batch) return;
      this.validating = true;
      try { this.report = await api('/batches/' + this.batch.id + '/validate'); }
      catch (e) { toast('校验失败：' + e.message, 'err'); }
      this.validating = false;
    },
    lazyValidate() {
      clearTimeout(this._vt);
      this._vt = setTimeout(() => this.runValidate(), 600);
    },
    /* --- 顶部条编辑 --- */
    async saveBatch(field) {
      try {
        await api('/batches/' + this.batch.id, { method: 'PUT', body: { [field]: this.batch[field] } });
        toast('已保存');
        if (field === 'factory_id') {
          const f = store.factories.find(x => x.id === this.batch.factory_id);
          this.batch.factory_name = f ? f.name : '';
        }
        this.lazyValidate();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
    },
    /* --- 重新同步 --- */
    async resync() {
      if (this.isOffline) {                 // 离线批次走重新上传发货单 Excel
        toast('离线批次请重新上传发货单 Excel');
        this.offlineOpen = true;
        return;
      }
      this.resyncBusy = true;
      try {
        const r = await api('/sync/import', { method: 'POST', body: { inbound_plan_id: this.batch.inbound_plan_id } });
        await this.reload(false);
        if (r && r.report) this.report = r.report;
        const n = ((r && r.report && r.report.errors) || []).length;
        toast('重新同步完成（人工修改字段已保护）' + (n ? '，校验提示 ' + n + ' 项' : ''));
      } catch (e) { toast('重新同步失败：' + e.message, 'err'); }
      this.resyncBusy = false;
    },
    async offlineImport(ev) {
      const f = ev.target.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f);
      fd.append('batch_name', this.batch.name || '');
      this.offlineBusy = true;
      try {
        const r = await api('/sync/import-excel', { method: 'POST', body: fd });
        this.offlineOpen = false;
        if (r && r.batch_id && r.batch_id !== this.batch.id) {
          toast('文件与本批次不一致，已导入为' + (r.created ? '新' : '另一') + '批次，正在跳转');
          location.hash = '#batches/' + r.batch_id;
        } else {
          await this.reload(false);
          if (r && r.report) this.report = r.report;
          toast('重新导入完成（人工修改字段已保护）');
        }
      } catch (e) { toast('重新导入失败：' + e.message, 'err'); }
      this.offlineBusy = false;
      ev.target.value = '';
    },
    /* --- 生成前数据预览 --- */
    async loadFieldMap() {
      if (this.fieldMap) return;
      try {
        if (!store.fields) store.fields = normalizeFields(await api('/fields'));
        const m = {};
        for (const g of store.fields) for (const f of g.items) m[f.path] = f.label || '';
        this.fieldMap = m;
      } catch (e) { this.fieldMap = {}; }   // 取不到中文名时降级显示路径
    },
    fieldLabel(p) {
      if (typeof p === 'string' && p.startsWith('text:')) return '固定文字';
      if (typeof p === 'string' && p.startsWith('num:')) return '固定数值';
      if (typeof p === 'string' && p.includes('{')) return '格式串';
      return (this.fieldMap && this.fieldMap[p]) || '';
    },
    fmtVal(v) { return (v === null || v === undefined) ? '' : String(v); },
    async openPreview(t) {
      if (this.previewBusyId) return;
      this.previewBusyId = t.id;
      try {
        await this.loadFieldMap();
        const r = await api('/batches/' + this.batch.id + '/preview?template_id=' + t.id);
        this.previewData = r;
        this.previewTpl = t;
        this.previewTab = 0;
      } catch (e) { toast('预览失败：' + e.message, 'err'); }
      this.previewBusyId = null;
    },
    /* --- 货件 / 明细 --- */
    addr(s) {
      return [s.address_line1, s.address_line2, s.city, s.state, s.postal_code, s.country_code]
        .filter(Boolean).join(', ');
    },
    shipQty(s) { return (s.items || []).reduce((a, i) => a + (i.qty || 0), 0); },
    dims(it) {
      if (it.carton_l == null && it.carton_w == null && it.carton_h == null) return '';
      return [it.carton_l, it.carton_w, it.carton_h].map(v => v == null ? '' : v).join('×');
    },
    amount(it) {
      if (it.qty == null || it.customs_unit_price == null) return '-';
      return (it.qty * it.customs_unit_price).toFixed(2);
    },
    async saveItem(it, field) {
      try {
        await api('/shipment-items/' + it.id, { method: 'PUT', body: { [field]: it[field] } });
        toast('已保存');
        this.lazyValidate();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
    },
    async saveDims(it, text) {
      const parts = String(text).split(/[×xX*,，\s]+/).filter(Boolean).map(Number);
      if (parts.some(isNaN) || !parts.length) { toast('箱规格式应为 长×宽×高，如 60×40×30', 'err'); return; }
      const body = { carton_l: parts[0] ?? null, carton_w: parts[1] ?? null, carton_h: parts[2] ?? null };
      try {
        await api('/shipment-items/' + it.id, { method: 'PUT', body });
        Object.assign(it, body);
        toast('已保存');
        this.lazyValidate();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
    },
    async saveShipment(s, field) {
      try {
        await api('/shipments/' + s.id, { method: 'PUT', body: { [field]: s[field] } });
        toast('已保存');
        this.lazyValidate();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
    },
    toggleBoxes(s) { this.boxOpen[s.id] = !this.boxOpen[s.id]; },
    /* --- 生成 --- */
    lockReason(t) {
      if (!t.requires_forwarder_no) return '';
      const miss = ((this.batch && this.batch.shipments) || []).filter(s => !s.forwarder_order_no).length;
      return miss ? miss + ' 个货件缺货代单号，生成时将跳过' : '';
    },
    async doGenerate() {
      this.generating = true; this.genErrors = [];
      try {
        const r = await api('/batches/' + this.batch.id + '/generate',
          { method: 'POST', body: { template_ids: this.selectedTplIds } });
        if (r && r.blocked) {
          this.report = { passed: false, errors: (r.report && r.report.errors) || r.errors || [] };
          toast('校验未通过，已阻止生成', 'err');
        } else {
          this.genErrors = (r && r.errors) || [];
          toast('生成完成：' + ((r && r.generated) || []).length + ' 个文件');
          await this.reload(false);
          this.runValidate();
        }
      } catch (e) { toast('生成失败：' + e.message, 'err'); }
      this.generating = false;
    },
    genErrText(e) {
      if (typeof e === 'string') return e;
      return e.msg || e.detail || e.error || JSON.stringify(e);
    },
    /* --- 文件 --- */
    shipLabel(sid) {
      if (!sid) return '批次级';
      const s = ((this.batch && this.batch.shipments) || []).find(x => x.id === sid);
      return s ? (s.fc_code || s.amazon_shipment_id) : '#' + sid;
    },
    async delDoc(d) {
      if (!confirm('删除文件「' + d.filename + '」？')) return;
      try {
        await api('/docs/' + (d.id ?? d.doc_id), { method: 'DELETE' });
        this.batch.docs = this.batch.docs.filter(x => x !== d);
        toast('已删除');
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
  },
};

/* ============ [4] 产品库 ============ */
const PRODUCT_REQUIRED = [
  ['name_customs_cn', '中文报关名'], ['name_customs_en', '英文报关名'],
  ['name_contract', '合同用品名'], ['name_invoice', '发票箱单名'], ['name_usage', '用途功能名'],
  ['hs_code', 'HS编码'], ['material', '材质'], ['declare_elements', '申报要素'],
  ['box_weight_kg', '单箱重量'],
];
const BLANK_PRODUCT = {
  sku: '', name_customs_cn: '', name_customs_en: '', name_contract: '', name_invoice: '',
  name_usage: '', hs_code: '', declare_elements: '', material: '', usage: '', brand_name: '',
  model: '', box_weight_kg: null, unit_price_default: null, purchase_cost_default: null,
  currency: 'USD', image_url: '', remark: '',
};

const PageProducts = {
  template: '#tpl-products',
  props: ['arg'],
  data() {
    return { list: [], q: '', loading: true, editing: null, saving: false, importBusy: false };
  },
  computed: {
    filtered() {
      const q = this.q.trim().toLowerCase();
      if (!q) return this.list;
      return this.list.filter(p =>
        (p.sku || '').toLowerCase().includes(q) ||
        (p.name_customs_cn || '').toLowerCase().includes(q) ||
        (p.name_customs_en || '').toLowerCase().includes(q) ||
        (p.name_invoice || '').toLowerCase().includes(q));
    },
  },
  async mounted() {
    await this.reload();
    if (this.arg) {                          // #products/12 → 直达编辑（校验"去修复"）
      const p = this.list.find(x => String(x.id) === String(this.arg));
      if (p) this.openEdit(p);
      else toast('未找到产品 #' + this.arg, 'err');
    }
  },
  methods: {
    async reload() {
      this.loading = true;
      try { this.list = (await api('/products')) || []; }
      catch (e) { toast('加载产品失败：' + e.message, 'err'); }
      this.loading = false;
    },
    missing(p) {
      return PRODUCT_REQUIRED
        .filter(([k]) => p[k] === null || p[k] === undefined || p[k] === '')
        .map(([, label]) => label);
    },
    openEdit(p) { this.editing = Object.assign({}, BLANK_PRODUCT, p || {}); },
    async save() {
      if (!this.editing.sku) { toast('SKU 必填', 'err'); return; }
      this.saving = true;
      try {
        if (this.editing.id) await api('/products/' + this.editing.id, { method: 'PUT', body: this.editing });
        else await api('/products', { method: 'POST', body: this.editing });
        toast('已保存');
        this.editing = null;
        await this.reload();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      this.saving = false;
    },
    async del(p) {
      if (!confirm('删除产品「' + p.sku + '」？')) return;
      try { await api('/products/' + p.id, { method: 'DELETE' }); toast('已删除'); this.reload(); }
      catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    async onImport(ev) {
      const f = ev.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      this.importBusy = true;
      try {
        const r = await api('/products/import', { method: 'POST', body: fd });
        toast('导入完成：新增 ' + (r.created ?? 0) + '，更新 ' + (r.updated ?? 0));
        await this.reload();
      } catch (e) { toast('导入失败：' + e.message, 'err'); }
      this.importBusy = false;
      ev.target.value = '';
    },
  },
};

/* ============ [5] 工厂管理 ============ */
const BLANK_FACTORY = { name: '', abbr: '', address: '', phone: '', contact: '', tax_no: '', bank_info: '', remark: '' };

const PageFactories = {
  template: '#tpl-factories',
  data() { return { list: [], loading: true, editing: null, saving: false }; },
  async mounted() { await this.reload(); },
  methods: {
    async reload() {
      this.loading = true;
      await ensure('factories', true);
      this.list = store.factories;
      this.loading = false;
    },
    openEdit(f) { this.editing = Object.assign({}, BLANK_FACTORY, f || {}); },
    async save() {
      if (!this.editing.name) { toast('名称必填', 'err'); return; }
      this.saving = true;
      try {
        if (this.editing.id) await api('/factories/' + this.editing.id, { method: 'PUT', body: this.editing });
        else await api('/factories', { method: 'POST', body: this.editing });
        toast('已保存');
        this.editing = null;
        await this.reload();
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      this.saving = false;
    },
    async del(f) {
      if (!confirm('删除工厂「' + f.name + '」？被历史批次引用时将改为停用。')) return;
      try {
        const r = await api('/factories/' + f.id, { method: 'DELETE' });
        if (r && r.deactivated) toast('已有引用，已改为停用');
        else toast('已删除');
        await this.reload();
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    async enable(f) {
      try { await api('/factories/' + f.id, { method: 'PUT', body: { active: true } }); toast('已重新启用'); this.reload(); }
      catch (e) { toast('操作失败：' + e.message, 'err'); }
    },
  },
};

/* ============ [6] 主体与品牌 ============ */
const BLANK_COMPANY = {
  type: 'shop', name_cn: '', name_en: '', address_cn: '', address_en: '', abbr: '', uscc: '',
  customs_code: '', phone: '', contact: '', bank_info: '', insurance_factor: 1.0,
  export_via_trade: false, default_port: '宁波', default_origin_place: '杭州', remark: '', doc_types: null,
};
const BLANK_BRAND = {
  name: '', abbr2: '', factory_id: null, company_id: null,
  doc_no_rule_shipment: '{sa}{brand2}-{fc}-{date}',
  doc_no_rule_purchase: '{sa}{brand2}-{country}-{date}', remark: '',
};

const PageCompanies = {
  template: '#tpl-companies',
  props: ['arg'],
  data() {
    return {
      tab: ['shop', 'trade', 'brands'].includes(this.arg) ? this.arg : 'shop',
      companyForm: null, brandForm: null, saving: false, importBusy: false,
      companyDocTypes: COMPANY_DOC_TYPES, companyDocSel: [],
    };
  },
  computed: {
    companyList() { return store.companies.filter(c => (c.type || 'shop') === this.tab); },
    shopCompanies() { return store.companies.filter(c => (c.type || 'shop') === 'shop' && c.active !== false); },
  },
  async mounted() {
    ensure('companies', true); ensure('brands', true); ensure('factories');
  },
  methods: {
    setTab(t) { location.hash = '#companies/' + t; },
    nameOf(res, id) {
      const r = (store[res] || []).find(x => x.id === id);
      return r ? (r.name || r.name_cn || '') : (id ? '#' + id : '-');
    },
    /* --- 主体 --- */
    openCompany(c) {
      this.companyForm = Object.assign({}, BLANK_COMPANY, c || {}, c ? {} : { type: this.tab });
      // doc_types 存为 JSON 字符串，读取时 parse
      let sel = [];
      try { sel = JSON.parse((c && c.doc_types) || '[]'); } catch (e) { sel = []; }
      this.companyDocSel = Array.isArray(sel) ? sel : [];
    },
    async saveCompany() {
      if (!this.companyForm.name_cn) { toast('中文名必填', 'err'); return; }
      // 仅店铺主体写入默认文件清单：选中数组 JSON.stringify，空数组存 null（=不限）
      if (this.companyForm.type === 'shop') {
        this.companyForm.doc_types = this.companyDocSel.length ? JSON.stringify(this.companyDocSel) : null;
      }
      this.saving = true;
      try {
        if (this.companyForm.id) await api('/companies/' + this.companyForm.id, { method: 'PUT', body: this.companyForm });
        else await api('/companies', { method: 'POST', body: this.companyForm });
        toast('已保存');
        this.companyForm = null;
        await ensure('companies', true);
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      this.saving = false;
    },
    async delCompany(c) {
      if (!confirm('删除主体「' + c.name_cn + '」？被引用时将改为停用。')) return;
      try {
        const r = await api('/companies/' + c.id, { method: 'DELETE' });
        if (r && r.deactivated) toast('已有引用，已改为停用');
        else toast('已删除');
        await ensure('companies', true);
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    async enableCompany(c) {
      try { await api('/companies/' + c.id, { method: 'PUT', body: { active: true } }); toast('已重新启用'); ensure('companies', true); }
      catch (e) { toast('操作失败：' + e.message, 'err'); }
    },
    /* --- 品牌 --- */
    openBrand(b) { this.brandForm = Object.assign({}, BLANK_BRAND, b || {}); },
    preview(rule) {
      const sample = {
        sa: 'SA-',
        brand2: ((this.brandForm && (this.brandForm.abbr2 || this.brandForm.name)) || 'SE').slice(0, 2).toUpperCase(),
        fc: 'GYR2', date: '2026-05-19', country: 'US', shipdate: '2026-06-19',
      };
      return (rule || '').replace(/\{(\w+)\}/g, (m, k) => sample[k] !== undefined ? sample[k] : m);
    },
    async saveBrand() {
      if (!this.brandForm.name) { toast('品牌名必填', 'err'); return; }
      this.saving = true;
      try {
        if (this.brandForm.id) await api('/brands/' + this.brandForm.id, { method: 'PUT', body: this.brandForm });
        else await api('/brands', { method: 'POST', body: this.brandForm });
        toast('已保存');
        this.brandForm = null;
        await ensure('brands', true);
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      this.saving = false;
    },
    async delBrand(b) {
      if (!confirm('删除品牌「' + b.name + '」？被引用时将改为停用。')) return;
      try {
        const r = await api('/brands/' + b.id, { method: 'DELETE' });
        if (r && r.deactivated) toast('已有引用，已改为停用');
        else toast('已删除');
        await ensure('brands', true);
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    async enableBrand(b) {
      try { await api('/brands/' + b.id, { method: 'PUT', body: { active: true } }); toast('已重新启用'); ensure('brands', true); }
      catch (e) { toast('操作失败：' + e.message, 'err'); }
    },
    async onBrandImport(ev) {
      const f = ev.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      this.importBusy = true;
      try {
        const r = await api('/brands/import', { method: 'POST', body: fd });
        const summary = (r && typeof r === 'object')
          ? Object.entries(r).map(([k, v]) => k + '=' + v).join('，') : '';
        toast('导入完成' + (summary ? '：' + summary : ''));
        ensure('brands', true); ensure('factories', true); ensure('companies', true);
      } catch (e) { toast('导入失败：' + e.message, 'err'); }
      this.importBusy = false;
      ev.target.value = '';
    },
  },
};

/* ============ [7] 货代 & 模板（三步向导） ============ */
const BLANK_FORWARDER = { name: '', sellfox_name: '', contact: '', phone: '', remark: '' };
const BLANK_TEMPLATE = {
  id: null, name: '', doc_type: '托书', owner_type: 'forwarder', forwarder_id: null,
  granularity: 'shipment', filename_rule: '', requires_forwarder_no: false,
  stored_file: '', original_name: '', active: false,
};

/** GET /api/fields 返回结构归一化 → [{ns, items:[{path,label}]}] */
function normalizeFields(res) {
  const groups = {};
  const add = (path, label) => {
    if (!path) return;
    const ns = String(path).split('.')[0];
    (groups[ns] = groups[ns] || []).push({ path, label: label || '' });
  };
  if (Array.isArray(res)) {
    res.forEach(f => {
      if (typeof f === 'string') add(f);
      else add(f.path || f.key || f.name, f.label || f.desc || f.cn || '');
    });
  } else if (res && typeof res === 'object') {
    for (const [k, v] of Object.entries(res)) {
      if (Array.isArray(v)) {
        v.forEach(f => {
          if (typeof f === 'string') add(f.includes('.') ? f : k + '.' + f);
          else add(f.path || f.key || ((f.name && !String(f.name).includes('.')) ? k + '.' + f.name : f.name),
                   f.label || f.desc || f.cn || '');
        });
      } else if (v && typeof v === 'object') {
        for (const [kk, vv] of Object.entries(v)) add(kk.includes('.') ? kk : k + '.' + kk, String(vv));
      }
    }
  }
  const order = ['batch', 'shipment', 'item', 'box', 'company', 'factory', 'trade', 'forwarder', 'calc', 'meta'];
  return Object.entries(groups)
    .sort((a, b) => {
      const ia = order.indexOf(a[0]), ib = order.indexOf(b[0]);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    })
    .map(([ns, items]) => ({ ns, items }));
}

const PageForwarders = {
  template: '#tpl-forwarders',
  data() {
    return {
      fwdForm: null, saving: false,
      tplQuery: '',
      wizOpen: false, wizStep: 1, wiz: Object.assign({}, BLANK_TEMPLATE),
      mappingText: '', sheetnames: [], confidences: [], aiNotes: '',
      uploadBusy: false, aiBusy: false, testBusy: false, testBatchId: null,
      fieldGroups: [], fieldsCollapsed: {}, docTypes: DOC_TYPES,
      advOpen: false, autoAiDraft: true,
      /* 步骤2 可视化映射 */
      mapMode: 'visual', selectedAddr: null,
      gridLoading: false, gridError: '', gridSheets: [], gridTab: 0,
      /* 模板预览弹窗 */
      previewTpl: null, previewMapping: null,
    };
  },
  computed: {
    /* 模板列表：按归属分组 + 搜索过滤 + 组内按 doc_type 排序 */
    tplGroups() {
      const q = this.tplQuery.trim().toLowerCase();
      const match = (t) => {
        if (!q) return true;
        return [t.name, t.doc_type, this.ownerLabel(t)]
          .some(x => String(x || '').toLowerCase().includes(q));
      };
      const dtOrder = DOC_TYPES;
      const groups = [];
      const byKey = {};
      const pushTo = (key, label, internal) => {
        if (!byKey[key]) { byKey[key] = { key, label, internal, list: [] }; groups.push(byKey[key]); }
        return byKey[key];
      };
      for (const t of this.store.templates) {
        if (!match(t)) continue;
        if (t.owner_type === 'internal') pushTo('__internal', '内部模板', true).list.push(t);
        else {
          const lbl = this.ownerLabel(t);
          pushTo('fwd:' + (t.forwarder_id ?? lbl), lbl, false).list.push(t);
        }
      }
      // 货代组在前（按名称），内部组置底
      groups.sort((a, b) => {
        if (a.internal !== b.internal) return a.internal ? 1 : -1;
        return String(a.label).localeCompare(String(b.label), 'zh');
      });
      const di = (t) => { const i = dtOrder.indexOf(t.doc_type); return i < 0 ? 99 : i; };
      groups.forEach(g => g.list.sort((a, b) => di(a) - di(b) || String(a.name).localeCompare(String(b.name), 'zh')));
      return groups;
    },
    curSheet() { return this.gridSheets[this.gridTab] || { rows: [], cols: [], cells: {} }; },
    /* 当前 mappingText 解析出的 cells 映射，按 sheet 名归类 → {sheetName: {addr: path}} */
    mappingCells() {
      const out = {};
      let obj;
      try { obj = JSON.parse(this.mappingText || '{}'); } catch (e) { return out; }
      const sheets = (obj && obj.sheets) || [];
      for (const s of sheets) {
        if (s && s.cells) out[s.name || ''] = s.cells;
      }
      return out;
    },
  },
  async mounted() { ensure('forwarders', true); ensure('templates', true); },
  methods: {
    ownerLabel(t) {
      if (t.owner_type === 'internal') return '内部';
      if (t.forwarder_name) return t.forwarder_name;
      const f = store.forwarders.find(x => x.id === t.forwarder_id);
      return f ? f.name : '货代';
    },
    /* --- 货代 CRUD --- */
    openFwd(f) { this.fwdForm = Object.assign({}, BLANK_FORWARDER, f || {}); },
    async saveFwd() {
      if (!this.fwdForm.name) { toast('名称必填', 'err'); return; }
      this.saving = true;
      try {
        if (this.fwdForm.id) await api('/forwarders/' + this.fwdForm.id, { method: 'PUT', body: this.fwdForm });
        else await api('/forwarders', { method: 'POST', body: this.fwdForm });
        toast('已保存');
        this.fwdForm = null;
        await ensure('forwarders', true);
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      this.saving = false;
    },
    async delFwd(f) {
      if (!confirm('删除货代「' + f.name + '」？')) return;
      try { await api('/forwarders/' + f.id, { method: 'DELETE' }); toast('已删除'); ensure('forwarders', true); }
      catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    /* --- 模板列表操作 --- */
    async toggleTpl(t) {
      try {
        await api('/templates/' + t.id, { method: 'PUT', body: { active: !t.active } });
        toast(t.active ? '已停用' : '已启用');
        ensure('templates', true);
      } catch (e) { toast('操作失败：' + e.message, 'err'); }
    },
    async delTpl(t) {
      if (!confirm('删除模板「' + t.name + '」？')) return;
      try { await api('/templates/' + t.id, { method: 'DELETE' }); toast('已删除'); ensure('templates', true); }
      catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
    /* --- 网格通用工具 --- */
    colLetter(n) {
      let s = '';
      while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - 1 - m) / 26; }
      return s;
    },
    cellVal(sh, r, c) {
      const v = sh.cells ? sh.cells[this.colLetter(c) + r] : undefined;
      return (v === null || v === undefined) ? '' : v;
    },
    /* 字段路径 → 中文标签（字段字典里有则取 label，否则取路径） */
    bindLabel(path) {
      if (typeof path === 'string' && path.startsWith('text:')) return '固定:' + path.slice(5);
      for (const g of this.fieldGroups) {
        const f = g.items.find(x => x.path === path);
        if (f && f.label) return f.label;
      }
      return path;
    },
    /* --- 模板预览（GET /templates/{id}/grid） --- */
    async openPreview(t) {
      this.previewTpl = t;
      this.gridSheets = []; this.gridTab = 0; this.gridError = ''; this.gridLoading = true;
      this.previewMapping = null;
      if (t && t.mapping) {
        try { this.previewMapping = JSON.parse(t.mapping); } catch (e) { this.previewMapping = null; }
      }
      try {
        const r = await api('/templates/' + t.id + '/grid');
        this.gridSheets = (r && r.sheets) || [];
      } catch (e) { this.gridError = e.message; }
      this.gridLoading = false;
    },
    /* 预览弹窗：某 sheet 是否有已配置映射的格 */
    previewMarks(sh) {
      const cells = this.previewMappingCells(sh.name);
      return cells && Object.keys(cells).length > 0;
    },
    previewMappingCells(sheetName) {
      const sheets = (this.previewMapping && this.previewMapping.sheets) || [];
      const s = sheets.find(x => (x.name || '') === sheetName) || sheets[0];
      return (s && s.cells) || {};
    },
    /* 预览弹窗：该格绑定的字段路径（用 previewMapping） */
    boundOf(sh, r, c) {
      const addr = this.colLetter(c) + r;
      if (this.previewTpl) return this.previewMappingCells(sh.name)[addr] || null;
      // 向导可视化模式：用当前 mappingText 解析结果
      const cells = this.mappingCells[sh.name] || this.mappingCells[''] || {};
      return cells[addr] || null;
    },
    cellTitle(sh, r, c) {
      const addr = this.colLetter(c) + r;
      const b = this.boundOf(sh, r, c);
      if (b) return addr + ' → ' + b;
      const v = this.cellVal(sh, r, c);
      return v ? addr + '：' + v : '点击绑定 ' + addr;
    },
    /* --- 向导 --- */
    openWizard(t) {
      this.wiz = Object.assign({}, BLANK_TEMPLATE, t || {});
      this.mappingText = '';
      if (t && t.mapping) {
        try { this.mappingText = JSON.stringify(JSON.parse(t.mapping), null, 2); }
        catch (e) { this.mappingText = t.mapping; }
      }
      this.sheetnames = []; this.confidences = []; this.aiNotes = '';
      this.wizStep = 1; this.testBatchId = null;
      this.advOpen = false;
      this.mapMode = 'visual'; this.selectedAddr = null;
      this.gridSheets = []; this.gridTab = 0; this.gridError = ''; this.gridLoading = false;
      this.previewTpl = null; this.previewMapping = null;
      this.wizOpen = true;
      this.loadFields();
      ensure('batches');
      // 编辑已存模板时预取网格，进步骤2即可点选
      if (t && (t.id || t.stored_file)) this.loadWizGrid();
    },
    closeWizard() { this.wizOpen = false; },
    async loadFields() {
      if (store.fields) { this.fieldGroups = store.fields; return; }
      try {
        store.fields = normalizeFields(await api('/fields'));
        this.fieldGroups = store.fields;
      } catch (e) { toast('加载字段字典失败：' + e.message, 'err'); }
    },
    /* 字段字典命名空间折叠（纯 UI 状态） */
    toggleFieldNs(ns) { this.fieldsCollapsed[ns] = !this.fieldsCollapsed[ns]; },
    async copyPath(p) {
      try { await navigator.clipboard.writeText(p); toast('已复制 ' + p); }
      catch (e) { toast('复制失败，请手动选择文本', 'err'); }
    },
    /* --- 步骤2 可视化点选映射 --- */
    switchMode(m) {
      this.mapMode = m;
      this.selectedAddr = null;
      if (m === 'visual' && !this.gridSheets.length && this.wiz.stored_file) this.loadWizGrid();
    },
    /* 向导内取模板网格：已存模板用 GET /grid，新上传用 POST /preview-grid */
    async loadWizGrid() {
      if (!this.wiz.stored_file && !this.wiz.id) { this.gridSheets = []; return; }
      this.gridLoading = true; this.gridError = ''; this.gridTab = 0;
      try {
        let r;
        if (this.wiz.stored_file) r = await api('/templates/preview-grid', { method: 'POST', body: { stored_file: this.wiz.stored_file } });
        else r = await api('/templates/' + this.wiz.id + '/grid');
        this.gridSheets = (r && r.sheets) || [];
      } catch (e) { this.gridError = e.message; this.gridSheets = []; }
      this.gridLoading = false;
    },
    /* 点选模板格 */
    pickCell(addr) {
      this.selectedAddr = (this.selectedAddr === addr) ? null : addr;
    },
    /* 字段面板点击：绑定模式=写映射；普通模式=复制路径 */
    onFieldClick(path) {
      if (this.selectedAddr) this.writeBinding(this.selectedAddr, path);
      else this.copyPath(path);
    },
    /* 固定文字绑定 */
    bindFixed() {
      if (!this.selectedAddr) return;
      const cur = this.currentCellBinding(this.selectedAddr);
      const def = (typeof cur === 'string' && cur.startsWith('text:')) ? cur.slice(5) : '';
      const v = prompt('为 ' + this.selectedAddr + ' 绑定固定文字（留空则解绑）：', def);
      if (v === null) return;
      if (v.trim() === '') this.writeBinding(this.selectedAddr, null);
      else this.writeBinding(this.selectedAddr, 'text:' + v);
    },
    currentCellBinding(addr) {
      const name = this.curSheet.name || '';
      const cells = this.mappingCells[name] || this.mappingCells[''] || {};
      return cells[addr] || null;
    },
    /* 写入/删除一格映射 → 唯一数据源 mappingText（JSON 字符串） */
    writeBinding(addr, path) {
      let obj;
      try { obj = JSON.parse(this.mappingText || '{}'); } catch (e) { obj = {}; }
      if (!obj || typeof obj !== 'object') obj = {};
      if (!Array.isArray(obj.sheets)) obj.sheets = [];
      const name = this.curSheet.name || (this.gridSheets[0] && this.gridSheets[0].name) || '';
      let sheet = obj.sheets.find(s => (s.name || '') === name);
      if (!sheet) { sheet = { name, cells: {} }; obj.sheets.push(sheet); }
      if (!sheet.cells || typeof sheet.cells !== 'object') sheet.cells = {};
      if (path === null) {
        delete sheet.cells[addr];
        toast('已解绑 ' + addr);
      } else {
        sheet.cells[addr] = path;
        toast(addr + ' → ' + path);
      }
      this.mappingText = JSON.stringify(obj, null, 2);
      this.selectedAddr = null;
    },
    async uploadFile(ev) {
      const f = ev.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      this.uploadBusy = true;
      try {
        const r = await api('/templates/upload-file', { method: 'POST', body: fd });
        this.wiz.stored_file = r.stored_file;
        this.wiz.original_name = r.original_name || f.name;
        this.sheetnames = r.sheetnames || [];
        toast('上传成功，共 ' + this.sheetnames.length + ' 个工作表');
        // 取模板网格供可视化点选
        this.gridSheets = []; this.gridTab = 0;
        this.loadWizGrid();
        // 简化流程：自动跳步骤2 + 触发一次 AI 草稿
        if (this.autoAiDraft) {
          this.wizStep = 2;
          this.mapMode = 'visual';
          this.aiDraft();
        }
      } catch (e) { toast('上传失败：' + e.message, 'err'); }
      this.uploadBusy = false;
      ev.target.value = '';
    },
    async aiDraft() {
      if (!this.wiz.stored_file) { toast('请先在步骤1上传模板文件', 'err'); return; }
      this.aiBusy = true;
      try {
        const r = await api('/templates/ai-mapping', { method: 'POST', body: { stored_file: this.wiz.stored_file } });
        if (r && r.error) toast('AI 映射：' + r.error, 'err');
        if (r && r.mapping) {
          this.mappingText = typeof r.mapping === 'string'
            ? r.mapping : JSON.stringify(r.mapping, null, 2);
        }
        this.confidences = this.normConfidences(r && r.confidences);
        this.aiNotes = (r && r.notes) || '';
        if (r && r.mapping) toast('AI 草稿已生成，请人工核对低置信度项');
      } catch (e) { toast('AI 映射失败：' + e.message, 'err'); }
      this.aiBusy = false;
    },
    normConfidences(c) {
      if (!c) return [];
      if (Array.isArray(c)) {
        return c.map(x => ({
          target: x.target || x.cell || x.addr || x.sheet || '',
          path: x.path || x.field || x.value || '',
          conf: Number(x.confidence ?? x.conf ?? x.score ?? 0),
        }));
      }
      return Object.entries(c).map(([k, v]) => ({
        target: k,
        path: (v && typeof v === 'object') ? (v.path || v.field || '') : '',
        conf: Number((v && typeof v === 'object') ? (v.confidence ?? v.conf ?? 0) : v),
      }));
    },
    /* 保存：mapping 非空必须是合法 JSON */
    buildPayload(activeFlag) {
      let mapping = this.mappingText.trim();
      if (mapping) {
        try { JSON.parse(mapping); }
        catch (e) { throw new Error('映射 JSON 解析失败：' + e.message); }
      }
      const p = {
        name: this.wiz.name, doc_type: this.wiz.doc_type, owner_type: this.wiz.owner_type,
        forwarder_id: this.wiz.owner_type === 'forwarder' ? this.wiz.forwarder_id : null,
        granularity: this.wiz.granularity, filename_rule: this.wiz.filename_rule,
        requires_forwarder_no: this.wiz.requires_forwarder_no,
        stored_file: this.wiz.stored_file, original_name: this.wiz.original_name,
        mapping,
      };
      if (activeFlag !== null) p.active = activeFlag;
      return p;
    },
    async ensureSaved(activeFlag) {
      if (!this.wiz.name) throw new Error('模板名称必填');
      const body = this.buildPayload(activeFlag);
      if (this.wiz.id) {
        await api('/templates/' + this.wiz.id, { method: 'PUT', body });
      } else {
        const r = await api('/templates', { method: 'POST', body });
        if (r && r.id) this.wiz.id = r.id;
        else {                       // 兜底：按名称回查 id
          await ensure('templates', true);
          const t = store.templates.find(x => x.name === this.wiz.name);
          if (t) this.wiz.id = t.id;
        }
      }
      await ensure('templates', true);
    },
    async saveWizard(active) {
      this.saving = true;
      try {
        await this.ensureSaved(active);
        toast(active ? '已保存并启用' : '已保存（未启用）');
        this.wizOpen = false;
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
      this.saving = false;
    },
    async testGen() {
      if (!this.testBatchId) { toast('请先选择批次', 'err'); return; }
      this.testBusy = true;
      try {
        await this.ensureSaved(this.wiz.id ? null : false);   // 试生成前需有模板 id
        const fname = await postDownload('/api/templates/' + this.wiz.id + '/test-generate',
          { batch_id: this.testBatchId });
        toast('试生成成功：' + fname);
      } catch (e) { toast('试生成失败：' + e.message, 'err'); }
      this.testBusy = false;
    },
  },
};

/* ============ [8] 规则配置 ============ */
function ruleGroupOf(r) {
  const k = (r.key || '') + ' ' + (r.label || '');
  if (/insur|保价|投保/i.test(k)) return '投保常量';
  if (/customs|declare|origin_country|dest_country|trade_mode|package|unit|currency|tax_mode|报关|原产|运抵|监管|征免|币制|包装/i.test(k)) return '报关常量';
  if (/date|day|friday|日期|基准日/i.test(k)) return '日期规则';
  return '计算系数';
}

const PageRules = {
  template: '#tpl-rules',
  data() { return { list: [], loading: true }; },
  computed: {
    groups() {
      const order = ['日期规则', '计算系数', '报关常量', '投保常量'];
      const m = {};
      for (const r of this.list) (m[ruleGroupOf(r)] = m[ruleGroupOf(r)] || []).push(r);
      return order.filter(l => m[l] && m[l].length).map(l => ({ label: l, list: m[l] }));
    },
  },
  async mounted() {
    try { this.list = (await api('/rule-configs')) || []; }
    catch (e) { toast('加载规则失败：' + e.message, 'err'); }
    this.loading = false;
  },
  methods: {
    /* 分组说明文案（纯展示） */
    groupDesc(label) {
      return {
        '日期规则': '合同日期、发货日等基准日期的推算规则',
        '计算系数': '重量、体积、金额等派生数值的计算系数',
        '报关常量': '报关单据中固定填写的申报口径常量',
        '投保常量': '投保单据中使用的保价与费率常量',
      }[label] || '';
    },
    async saveVal(r) {
      try {
        await api('/rule-configs/' + r.id, { method: 'PUT', body: { value: r.value } });
        toast('已保存「' + (r.label || r.key) + '」');
      } catch (e) { toast('保存失败：' + e.message, 'err'); }
    },
    restore(r) { r.value = r.default_value; this.saveVal(r); },
  },
};

/* ============ [9] 生成记录 ============ */
const PageDocs = {
  template: '#tpl-docs',
  data() {
    return {
      batchId: null, docs: [], shipments: [], loading: false,
      typeFilter: '', docTypes: DOC_TYPES, previewDoc: null,
    };
  },
  computed: {
    filtered() {
      return this.typeFilter ? this.docs.filter(d => d.doc_type === this.typeFilter) : this.docs;
    },
  },
  async mounted() { ensure('batches', true); },
  methods: {
    async load() {
      if (!this.batchId) { this.docs = []; return; }
      this.loading = true;
      try {
        const full = await api('/batches/' + this.batchId + '/full');
        this.docs = (full && full.docs) || [];
        this.shipments = (full && full.shipments) || [];
      } catch (e) { toast('加载失败：' + e.message, 'err'); this.docs = []; }
      this.loading = false;
    },
    shipLabel(sid) {
      if (!sid) return '批次级';
      const s = this.shipments.find(x => x.id === sid);
      return s ? (s.fc_code || s.amazon_shipment_id) : '#' + sid;
    },
    async del(d) {
      if (!confirm('删除文件「' + d.filename + '」？')) return;
      try {
        await api('/docs/' + (d.id ?? d.doc_id), { method: 'DELETE' });
        this.docs = this.docs.filter(x => x !== d);
        toast('已删除');
      } catch (e) { toast('删除失败：' + e.message, 'err'); }
    },
  },
};

/* ============ [9.5] Excel 文件预览组件（批次详情 / 生成记录共用） ============ */
const DocPreview = {
  props: ['docId', 'filename'],
  emits: ['close'],
  data() { return { loading: true, error: '', sheets: [], tab: 0 }; },
  async mounted() {
    try {
      const r = await api('/docs/' + this.docId + '/preview');
      this.sheets = (r && r.sheets) || [];
    } catch (e) { this.error = e.message; }
    this.loading = false;
  },
  methods: {
    colLetter(n) {
      let s = '';
      while (n > 0) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = (n - 1 - m) / 26; }
      return s;
    },
    cellVal(sh, r, c) {
      const v = sh.cells[this.colLetter(c) + r];
      return (v === null || v === undefined) ? '' : v;
    },
  },
  template: `
  <modal :title="'文件预览 — ' + (filename || '')" size="xl" @close="$emit('close')">
    <div v-if="loading" class="empty">读取文件中…</div>
    <div v-else-if="error" class="empty">预览失败：{{error}}</div>
    <template v-else-if="sheets.length">
      <div v-if="sheets.length>1" class="tabs">
        <a v-for="(s,i) in sheets" :key="s.name" tabindex="0" :class="{active: tab===i}"
           @click="tab=i" @keydown.enter="tab=i">{{s.name}}</a>
      </div>
      <template v-for="(s,i) in sheets" :key="s.name">
        <div v-if="tab===i">
          <div v-if="s.truncated" class="hint">内容较大，仅展示前 60 行 × 20 列</div>
          <div class="xl-wrap">
            <table class="xl-grid">
              <thead><tr><th></th><th v-for="c in s.cols" :key="c">{{colLetter(c)}}</th></tr></thead>
              <tbody>
                <tr v-for="r in s.rows" :key="r">
                  <th>{{r}}</th>
                  <td v-for="c in s.cols" :key="c" :title="String(cellVal(s,r,c))">{{cellVal(s,r,c)}}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>
    </template>
    <div v-else class="empty">空文件</div>
  </modal>`,
};

/* ============ [10] 根应用 + hash 路由 ============ */
const PAGES = ['batches', 'purchase', 'products', 'factories', 'companies', 'forwarders', 'rules', 'docs'];

const Root = {
  data() {
    return {
      page: 'batches', arg: '',
      menus: [
        { key: 'batches', label: '批次管理', icon: '📦' },
        { key: 'purchase', label: '采购确认', icon: '🏭' },
        { key: 'products', label: '产品库', icon: '🏷️' },
        { key: 'factories', label: '工厂管理', icon: '🏭' },
        { key: 'companies', label: '主体与品牌', icon: '🏢' },
        { key: 'forwarders', label: '货代 & 模板', icon: '🚚' },
        { key: 'rules', label: '规则配置', icon: '⚙️' },
        { key: 'docs', label: '生成记录', icon: '📄' },
      ],
    };
  },
  computed: {
    toasts() { return toasts; },
    /* 新布局 topbar 显示当前页名（仅供模板引用，不涉及数据逻辑） */
    pageTitle() {
      const m = this.menus.find(x => x.key === this.page);
      return m ? m.label : '';
    },
    view() {
      if (this.page === 'batches' && this.arg) return 'page-batch-detail';
      return 'page-' + this.page;
    },
  },
  created() {
    this.route();
    window.addEventListener('hashchange', () => this.route());
  },
  methods: {
    route() {
      const h = location.hash.replace(/^#\/?/, '') || 'batches';
      const [p, ...rest] = h.split('/');
      this.page = PAGES.includes(p) ? p : 'batches';
      this.arg = rest.join('/');
    },
  },
};

const app = createApp(Root);
app.component('modal', Modal);
app.component('doc-preview', DocPreview);
app.component('page-batches', PageBatches);
app.component('page-purchase', PagePurchase);
app.component('page-batch-detail', PageBatchDetail);
app.component('page-products', PageProducts);
app.component('page-factories', PageFactories);
app.component('page-companies', PageCompanies);
app.component('page-forwarders', PageForwarders);
app.component('page-rules', PageRules);
app.component('page-docs', PageDocs);

Object.assign(app.config.globalProperties, {
  api, toast, store, fmtTime, num, money, badgeCls, granLabel,
});
app.config.errorHandler = (err) => {
  console.error(err);
  toast('界面错误：' + (err && err.message || err), 'err');
};
app.mount('#app');
