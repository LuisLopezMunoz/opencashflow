from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from opencashflow.cli import _render_table


@pytest.mark.parametrize('show_ids', [False, True])
def test_balance_total_uses_second_line_without_widening_months(capsys, show_ids):
    section = NS(name='Saldo', section_type='balance')
    row = NS(id=1, name='SALDO INICIAL', sign='positive')
    periods = [NS(id=i, label=f'mes-{i}') for i in (1, 2)]
    result = {'sections': [{'section': section, 'rows': [{'row': row, 'cells': [
        NS(period_id=i, error=None, projected_value=Decimal(100), effective_source='rule')
        for i in (1, 2)
    ]}]}]}
    kwargs = dict(sheet=NS(id=1, name='Prueba', currency='CLP'), result=result,
                  periods=periods, unit='1', width=200, show_ids=show_ids,
                  anchor_period_id=1, real_values={1: Decimal(223253)},
                  real_label='Actual', balance_row_id=1)
    _render_table(**kwargs)
    original = capsys.readouterr().out.splitlines()
    _render_table(**kwargs, combined_total=Decimal(123456789))
    lines = capsys.readouterr().out.splitlines()
    row_index = next(i for i, line in enumerate(lines) if 'SALDO INICIAL' in line)
    assert '223.253' in lines[row_index]
    assert '(123.456.789)' not in lines[row_index]
    assert lines[row_index + 1].strip() == '(123.456.789)'
    assert lines[row_index].index('223.253') + len('223.253') == len(lines[row_index + 1])
    header = next(line for line in lines if 'mes-1' in line)
    old_header = next(line for line in original if 'mes-1' in line)
    assert header.index('mes-1') == old_header.index('mes-1')
