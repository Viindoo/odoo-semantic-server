# SPDX-License-Identifier: AGPL-3.0-or-later
"""An empty XML file defines nothing; it is not a parse failure (E2E-D3).

Real case: odoo 16.0 ``addons/mass_mailing/data/mass_mailing_data.xml`` is a
0-byte file upstream (not referenced by the manifest). lxml rejects it
("Document is empty"), and the XML parsers reported that as a failure, so
``mass_mailing@16.0`` was marked degraded - never entity-pruned, and
``lifecycle_attention`` set on every run with exit 0.

Rules protected:

* a 0-byte or whitespace-only XML file yields no nodes and no failure, for the
  view, report and QWeb parsers alike;
* a file whose content really does not parse still degrades the module
  (content failure, not transient);
* a file that cannot be read still degrades the module (transient failure) -
  "unreadable" is never mistaken for "empty".
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from src.indexer import parse_health, parser_qweb, parser_xml
from src.indexer.models import ModuleInfo

V = "16.0"

_PARSERS = {
    "views": parser_xml.parse_file,
    "reports": parser_xml.parse_reports_file,
    "qweb": parser_qweb.parse_file,
}


def _module(tmp_path: Path) -> ModuleInfo:
    return ModuleInfo(name="mass_mailing", odoo_version=V, repo="odoo",
                      path=str(tmp_path / "mass_mailing"), depends=["mail"])


def _parse(parser, path: Path, module: ModuleInfo):
    with parse_health.track(module.name, V) as health:
        nodes = parser(str(path), module)
    return nodes, health


@pytest.mark.parametrize("parser", sorted(_PARSERS))
@pytest.mark.parametrize("content", ["", "   \n\t\n"], ids=["zero_bytes", "whitespace_only"])
def test_empty_xml_file_yields_nothing_and_does_not_degrade_the_module(
    tmp_path, parser, content,
):
    data = tmp_path / "mass_mailing" / "data" / "mass_mailing_data.xml"
    data.parent.mkdir(parents=True)
    data.write_text(content)

    nodes, health = _parse(_PARSERS[parser], data, _module(tmp_path))

    assert nodes == []
    assert health.degraded is False, health.failures
    assert health.failures == []


@pytest.mark.parametrize("parser", sorted(_PARSERS))
def test_xml_with_broken_content_still_degrades_the_module(tmp_path, parser):
    # GUARD: pre-existing behaviour (B14 content failure)
    data = tmp_path / "mass_mailing" / "views" / "broken.xml"
    data.parent.mkdir(parents=True)
    data.write_text("<odoo><record id='x'")

    nodes, health = _parse(_PARSERS[parser], data, _module(tmp_path))

    assert nodes == []
    assert health.degraded is True
    assert health.transient is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a chmod 000 file")
@pytest.mark.parametrize("parser", sorted(_PARSERS))
def test_unreadable_xml_file_is_a_transient_failure_not_an_empty_file(tmp_path, parser):
    # GUARD: pre-existing behaviour (an OSError stays a transient failure)
    data = tmp_path / "mass_mailing" / "data" / "mass_mailing_data.xml"
    data.parent.mkdir(parents=True)
    data.write_text("")
    data.chmod(0)
    try:
        nodes, health = _parse(_PARSERS[parser], data, _module(tmp_path))
    finally:
        data.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert nodes == []
    assert health.degraded is True
    assert health.transient is True
