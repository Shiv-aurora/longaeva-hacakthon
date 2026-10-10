from zipfile import ZipFile

import pytest

from scripts.audit_db1b_fares import original_cells


def test_independent_market_partition_and_decimal_weights(tmp_path):
    # Four sampled passengers: outbound connection then nonstop return.
    text='600|UA|20131|3|4|JFK||31703|0|22|UA|A|UA|A|X|500|ORD||30977|0|41|UA|A|UA|A|X|1000|SFO|X|32457|0|91|UA|A|UA|A|Y|1500|JFK|X|31703|0|22\n'
    p=tmp_path/'native.zip'
    with ZipFile(p,'w') as z:z.writestr('db1b.public.201303.asc',text)
    cells,s=original_cells(p,'2013-Q1')
    mainline={k:v for k,v in cells.items() if k[1]=='mainline_online'}
    assert len(mainline)==2 and s['records']==1 and s['sample_ticket_passengers']==4
    assert sum(v['amount'] for v in mainline.values())==2400
    assert sum(v['miles'] for v in mainline.values())==12000
    assert sum(v['pax'] for v in mainline.values())==8
    assert sorted(k[6] for k in mainline)==[1,2]
    assert all(v['witnesses']==[1] and v['first']==v['last']==1 for v in mainline.values())


def test_independent_rejection_counts_and_marketing_operating_boundary(tmp_path):
    text='100|UA|20131|1|2|JFK||31703|0|22|OO|D|UA|A|X|500|ORD|X|30977|0|41\n'
    questionable=text.replace('JFK||','JFK|*|')
    p=tmp_path/'native.zip'
    with ZipFile(p,'w') as z:z.writestr('source.asc',text+questionable)
    cells,s=original_cells(p,'2013-Q1')
    assert {k[1] for k in cells}=={'ticketed_online'}
    assert s['quality_record_counts']==[{'carrier':'UA','flag':'questionable_dollar_value','records':1}]
    assert s['cohort_counts']==[{'carrier':'UA','cohort':'ticketed_online','sample_ticket_records':1,'sample_ticket_passengers':2}]


def test_independent_locked_gate_precedes_fare_quantity(tmp_path):
    p=tmp_path/'native.zip'
    with ZipFile(p,'w') as z:z.writestr('source.asc','poison|UA|20241|1|1|JFK||31703|0|22|UA|A|UA|A|X|500|ORD|X|30977|0|41\n')
    with pytest.raises(ValueError,match='locked'):original_cells(p,'2024-Q1')
