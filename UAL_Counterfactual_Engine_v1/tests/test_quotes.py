from datetime import date,timedelta
import pytest
from app.quotes import parse_quotes,route_diagnostic
from app.data import load_fuel,load_ual

HEADER='observed_on,departure_on,carrier,origin,destination,cabin,stops,total_price_usd,source_url\n'
def item(obs,lead=28,amount=399):
    dep=(date.fromisoformat(obs)+timedelta(days=lead)).isoformat()
    return f'{obs},{dep},UA,EWR,SFO,economy,0,{amount},https://www.united.com/\n'

def test_csv_requires_provenance_and_valid_dates():
    assert parse_quotes(HEADER+item('2026-09-21'))[0]['lead_days']==28
    with pytest.raises(ValueError):parse_quotes('bad,columns\nx,y\n')
    with pytest.raises(ValueError):parse_quotes(HEADER+item('2026-09-21').replace('https://www.united.com/','no_source'))
    with pytest.raises(ValueError):parse_quotes(HEADER+item('2026-09-21')+item('2026-09-21'))
    with pytest.raises(ValueError):parse_quotes(HEADER+item('2026-09-21',lead=-1))

def test_route_diagnostic_requires_usable_real_series():
    f=load_fuel();u=load_ual()
    assert route_diagnostic([],f,u)['ready'] is False
    one=parse_quotes(HEADER+item('2026-09-21'))
    assert route_diagnostic(one,f,u)['ready'] is False

def test_route_diagnostic_produces_descriptive_not_trading_signal():
    days=[x['date'] for x in load_fuel() if '2026-09-21'<=x['date']<='2026-10-06']
    assert len(days)>=8
    q=parse_quotes(HEADER+''.join(item(d,amount=390+i*2) for i,d in enumerate(days)))
    result=route_diagnostic(q,load_fuel(),load_ual(),'EWR-SFO')
    assert result['ready'] is True
    assert 'unvalidated' in result['status']
    assert result['fare_change_pct']>0
    assert result['first_date']<result['last_date']
