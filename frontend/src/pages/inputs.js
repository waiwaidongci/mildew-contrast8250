import {feedback, field} from '../components/common/fields.js';
import {empty, panel} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {handler} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const QUARANTINE_STATE = {open: '待补交', resolved: '已补入', rejected: '补交仍不合格'};

function countLine(detail) {
  return el('p', {class: 'filename'},
    `共 ${detail.total_count} 行 · 纳入 ${detail.accepted_count} 行 · `
    + `待隔离 ${detail.open_count} 行 · 拦截 ${detail.blocked_count ?? 0} 行 · 修订第 ${detail.revision} 版`);
}

function correctionForm(detail, error, refresh) {
  const submit = button('补交修正版', {type: 'submit', class: 'primary', 'data-testid': 'submit-correction'});
  return el('form', {class: 'fields', onSubmit: handler(submit, error, async event => {
    const data = new FormData(event.target);
    const file = data.get('correction');
    if (!file?.size) throw new Error('请选择修正版 CSV 文件');
    if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
    const result = await workspace.correct(detail.id, {filename: file.name, csv_text: await file.text()});
    error.textContent = `本次补入 ${result.summary.this_accepted} 条，拦截 ${result.summary.this_rejected} 条；`
      + `只补入原先隔离的单号，已纳入与重复补交不重复计数。`;
    event.target.reset();
    await refresh();
  })}, field({key: 'correction_name', label: '修正版说明（可选）', type: 'string', required: false, max_length: 120}),
    el('label', {class: 'field', for: 'correction'}, el('span', {}, '修正版 CSV'),
      el('input', {id: 'correction', type: 'file', name: 'correction', accept: '.csv,text/csv', required: true})),
    p('只接受原隔离清单中的单号；已纳入记录与重复执行补交不会重复计数。', 'subtle'), submit, error);
}

function quarantinePanel(detail, quarantine) {
  const rows = quarantine.map(item => ({
    ...item,
    reasons_text: item.reasons.join('；'),
    state_text: QUARANTINE_STATE[item.status] || item.status,
    source_text: item.source === 'correction' ? '补交' : '首次导入',
  }));
  const columns = [
    {key: 'line_no', label: 'CSV 行号'},
    {key: 'inspection_id', label: '巡检单号', render: row => row.inspection_id || '（无单号）'},
    {key: 'reasons_text', label: '隔离原因'},
    {key: 'state_text', label: '状态'},
    {key: 'source_text', label: '来源'},
  ];
  return panel('隔离记录（逐条原因，不计入分母）',
    p(`待补交 ${detail.open_count} 条，拦截 ${detail.blocked_count ?? 0} 条；每条不合格记录都保留原始行与原因。`, 'subtle'),
    table(columns, rows, {caption: '隔离记录', empty: '没有隔离记录，本批次全部合格。', pageSize: 8}));
}

export async function inputs(route, refresh) {
  const {spec, datasets} = workspace;
  const selected = datasets.find(item => item.id === route.id) || datasets[0];
  const error = feedback();
  const sample = button('下载示例 CSV', {class: 'quiet', onClick: event => handler(sample, error, () => workspace.download('/template', spec.sample_name))(event)});
  const detail = selected && await workspace.getDataset(selected.id, route.offset);
  const fullDetail = selected && await workspace.getQuarantine(selected.id);
  const columns = spec.fields.map(field => ({key: field.key, label: field.label}));
  const preview = detail ? panel('批次明细（仅纳入合格记录）', el('div', {class: 'actions'},
    el('label', {for: 'dataset-id', class: 'sr'}, '选择数据批次'),
    el('select', {id: 'dataset-id', 'aria-label': '选择数据批次', onChange: event => { location.hash = `#inputs/${event.target.value}`; }},
      datasets.map(item => el('option', {value: item.id, selected: item.id === selected.id}, item.name))),
    link('使用此批次运行', `#run/${detail.id}`, {class: 'quiet'})),
    p(`${detail.filename} · 纳入 ${detail.row_count} 行 · ${date(detail.created_at)}`, 'filename'),
    countLine(detail),
    table(columns, detail.rows, {caption: '批次明细', pageSize: 16}),
    el('div', {class: 'pagination'},
      route.offset > 0 && link('上一批明细', `#inputs/${detail.id}/${Math.max(0, route.offset - 16)}`, {class: 'quiet'}),
      p(`${detail.rows.length ? route.offset + 1 : route.offset}–${route.offset + detail.rows.length} / ${detail.row_count}`),
      route.offset + 16 < detail.row_count && link('下一批明细', `#inputs/${detail.id}/${route.offset + 16}`, {class: 'quiet'})))
    : empty('没有可用的批次。请导入示例或业务 CSV。');
  const submit = button('校验并导入', {type: 'submit', class: 'primary'});
  const upload = session.canWrite && el('form', {class: 'fields', onSubmit: handler(submit, error, async event => {
    const data = new FormData(event.target);
    const file = data.get('source');
    if (!file?.size) throw new Error('请选择一个 CSV 文件');
    if (file.size > 2_000_000) throw new Error('CSV 最多 2 MB');
    const item = await workspace.upload({name: data.get('batch_name'), filename: file.name, csv_text: await file.text()});
    error.textContent = `导入完成：共 ${item.total_count} 行，纳入 ${item.accepted_count} 行，`
      + `待隔离 ${item.open_count} 行，拦截 ${item.blocked_count ?? 0} 行。不合格记录已逐条隔离。`;
    const target = `#inputs/${item.id}`;
    if (location.hash === target) await refresh();
    else location.hash = target;
  })}, field({key: 'batch_name', label: '批次名称', type: 'string', max_length: 120}),
    el('label', {class: 'field', for: 'source'}, el('span', {}, '业务 CSV'), el('input', {id: 'source', type: 'file', name: 'source', accept: '.csv,text/csv', required: true})),
    p('缺列、湿度越界或重复单号的记录会逐条隔离并登记原因，合格记录正常纳入，不影响整批筛查。', 'subtle'), submit, error);
  return el('div', {class: 'two-col'}, el('div', {class: 'wide-panel'}, preview,
    fullDetail && quarantinePanel(detail, fullDetail.quarantine),
    session.canWrite && fullDetail && detail.open_count > 0 && panel('补交修正版', correctionForm(detail, feedback(), refresh))),
    el('aside', {class: 'sidebar-panel'},
      panel('数据导入', p('导入后保留原批次和校验指纹，可用同一份数据复跑。', 'subtle'), sample, upload),
      panel('字段说明', el('dl', {class: 'dictionary'}, spec.fields.map(item => el('div', {}, el('dt', {}, item.label),
        el('dd', {}, item.key, item.required === false ? ' · 可空' : ' · 必填', item.options ? ` · ${item.options.join(' / ')}` : '')))))));
}
