"""An independent, offline check of the A02 evidence visible to a reviewer."""
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
import re
import sys

A02_PACKET = ('materials/packages/tiangong-package-gap-global-main-20261110/apple-scm-reuse-20261110/'
              'apple/material-dispatch-20261115/program-21ccbda17c0c8229/reviews/'
              'program-review:ede0dc04424a1211e71483bac9938b5f/packet.json')


def read_exact(ref):
    raw = Path(ref['path']).read_bytes()
    assert sha256(raw).hexdigest() == ref['sha256'], ref['path']
    return raw


def verify(facts):
    feralpi, furnes = facts['processes']
    source = {Path(ref['id']).name: read_exact(ref).decode()
              for ref in facts['original_artifacts'] if ref['id'].endswith('.txt')}
    for process in (feralpi, furnes):
        assert process['readback']['status'] == 'complete_match'
        assert not process['readback']['changed_pointers']
        read_exact(process['readback']['body_ref'])
        assert process['basis']['reference_exchanges'][0]['resulting_amount'] in ('1', 1)

    by_id = {str(row['id']): row for row in feralpi['exchanges']}
    flux, alloy = by_id['2'], by_id['3']
    assert flux['flow_ref']['@refObjectId'] == alloy['flow_ref']['@refObjectId']
    assert flux['flow_ref']['@version'] == alloy['flow_ref']['@version']
    assert 'Ferroalloy' in json.dumps(flux['flow_ref'])
    flow_key = 'flow/' + flux['flow_ref']['@refObjectId'] + '/' + flux['flow_ref']['@version']
    assert feralpi['shared_flow_references'][flow_key] == ['2', '3']
    flow = json.loads(read_exact(facts['verified_flow_reads'][flow_key]['body_ref']))
    if 'data' in flow:
        flow = flow['data']['content']
    flow = flow.get('json_ordered', flow)['flowDataSet']['flowInformation']['dataSetInformation']
    assert 'ferroalloy' in json.dumps(flow['name']).lower()
    frozen = json.loads(read_exact(feralpi['frozen_ref']))['processDataSet']
    assert 'Flux (lime' in frozen['exchanges']['exchange'][2]['generalComment'][0]['#text']
    assert (Decimal(flux['resulting_amount']), Decimal(alloy['resulting_amount'])) == (Decimal('0.043116'), Decimal('0.014406'))

    emas = source['feralpi-umwelterklaerung-2026.pdf.txt']
    epd = source['feralpi-epd.pdf.txt']
    assert re.search(r'Halbzeuge Stahlwerk \(Knüppel\).*931\.933', emas)
    assert re.search(r'products covered by this EPD.*81 163 t', epd)
    annual = feralpi['annual_production'][0]['#text']
    assert all(text in annual for text in ('1000 kg/year reference-quantity substitute', '81 163 t', '931 933 t'))
    assert '/processDataSet/processInformation/technology' in feralpi['field_pointers']
    tech = frozen['processInformation']['technology']['technologyDescriptionAndIncludedProcesses'][0]['#text']
    assert 'arc furnace' in tech and 'continuous casting' in tech
    assert 'Hauptverbraucher für Strom ist der E-Ofen' in emas
    assert 'Hauptverbraucher für Erdgas ist der Hubherdofen' in emas

    by_id = {str(row['id']): row for row in furnes['exchanges']}
    epd = source['furnes-epd.pdf.txt']
    table = epd.split('Materials kg %', 1)[1].split('Sum product', 1)[0]
    source_amounts = [Decimal(amount.replace(',', '.')) for amount in re.findall(r'(\d+(?:,\d+)?)\s+\d+,\d+\s*%', table)]
    assert [Decimal(by_id[str(i)]['resulting_amount']) for i in range(1, 7)] == [v/1000 for v in source_amounts]
    assert len(source_amounts) == 6 and sum(source_amounts) == 1025
    assert 'Sum product 1000 100%' in epd
    assert len(re.findall(r'1 538,4', epd)) == 2  # Two characterizations of one electricity entry.
    assert Decimal(by_id['7']['resulting_amount']) == Decimal('1538.4')*Decimal('3.6')/1000
    assert 'declaration is valid for all products of ductile cast iron' in epd
    return 'A02-facts-verified'


if __name__ == '__main__':
    print(verify(json.loads(Path(sys.argv[1]).read_bytes())))
