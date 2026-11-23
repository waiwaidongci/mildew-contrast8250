import csv
from src.config.settings import ARTIFACTS


def safe_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                                   or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


def write_report(run_id, attempt, result, quarantine=None):
    """写出结果 CSV。quarantine 来自运行快照，保证报告与该次结果同口径、不可变。"""
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'analysis-{run_id}-{attempt}.csv'
    path = ARTIFACTS / name
    staging = path.with_suffix('.tmp')
    state_label = {'open': '待补交', 'resolved': '已补入', 'rejected': '补交仍不合格'}
    source_label = {'import': '首次导入', 'correction': '补交'}
    with staging.open('w', encoding='utf-8-sig', newline='') as output:
        writer = csv.writer(output)
        writer.writerow([safe_cell(result['headline'])])
        writer.writerow([safe_cell(result.get('note', ''))])
        for metric in result['metrics']:
            writer.writerow([safe_cell(metric['label']), safe_cell(metric['value']), safe_cell(metric.get('detail', ''))])

        quality = result.get('quality')
        if quality:
            writer.writerow([])
            writer.writerow(['数据质检口径'])
            writer.writerow(['总行数', '纳入数', '待补交隔离', '拦截', '说明'])
            writer.writerow([quality['total_count'], quality['accepted_count'],
                             quality.get('open_count', quality['quarantined_count']),
                             quality.get('blocked_count', 0),
                             safe_cell(quality['denominator_note'])])

        if quarantine:
            writer.writerow([])
            writer.writerow(['隔离记录明细（逐条原因，不计入支持度分母）'])
            writer.writerow(['CSV 行号', '巡检单号', '隔离原因', '快照时状态', '来源'])
            for item in quarantine:
                writer.writerow([item['line_no'], safe_cell(item['inspection_id']),
                                 safe_cell('；'.join(item['reasons'])),
                                 state_label.get(item['status'], item['status']),
                                 source_label.get(item['source'], item['source'])])

        for table in result['tables']:
            writer.writerow([])
            writer.writerow([safe_cell(table['title'])])
            columns = table['columns']
            writer.writerow([safe_cell(column['label']) for column in columns])
            writer.writerows([safe_cell(row.get(column['key'])) for column in columns] for row in table['rows'])
    staging.replace(path)
    return name
