from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from django.utils import timezone
from lxml import etree as ET

from arkumu.metadata.models.resource import PublicAccessLevel, Resource, ResourceType
from arkumu.oaipmh import views as oai_views
from arkumu.oaipmh.models import OAIProjectMediaLink, OAIProjectPublication
from arkumu.oaipmh.views import base as base_views
from arkumu.oaipmh.views import harvest as harvest_views
from arkumu.oaipmh.views import projects as project_views
from arkumu.oaipmh.views import tailored as tailored_views
from arkumu.oaipmh.views.config import METS_NS
from arkumu.projects import ProjectActor, ProjectDigitalObject, ProjectInstitution, ProjectRecord
from arkumu.users.models import Organization, User
from arkumu.storage.models.s3_file_objects import S3FileObject
from arkumu.metadata.models.triples import Triple
from arkumu.oaipmh.models import OAIDcpPathIndex
from arkumu.catalog.services.project_views import ProjectURIs
from arkumu.oaipmh.oai_project_tailored import OAIProjectBuilderTailored


@pytest.mark.django_db
def test_tailored_endpoint_curates_media_links(client, monkeypatch, settings):
    settings.OAI_BASIC_AUTH_ENABLED = False
    settings.OAI_ROSETTA_HARVESTABLE_ORGS = ()
    settings.OAI_S3_HARVESTABLE_ORGS = ()

    user = User.objects.create_user(username="oai-admin", password="test", role="system_admin")
    client.force_login(user)

    org = Organization.objects.create(name="FUK", code="fuk", is_active=True)
    base_ts = timezone.now() - timedelta(days=30)
    project = Resource.objects.create(
        uri="https://arkumu.org/entities/projekt/100",
        organization=org,
        resource_type=ResourceType.ENTITY,
        public_access_level=PublicAccessLevel.PUBLIC,
        is_public_approved=True,
    )
    Resource.objects.filter(pk=project.pk).update(updated_at=base_ts)

    def _make_digital(slug: str) -> Resource:
        return Resource.objects.create(
            uri=f"https://arkumu.org/entities/digital/{slug}",
            organization=org,
            resource_type=ResourceType.ENTITY,
            public_access_level=PublicAccessLevel.PUBLIC,
            is_public_approved=True,
        )

    digital_extra = _make_digital("extra")
    digital_a = _make_digital("a")
    digital_b = _make_digital("b")
    S3FileObject.objects.create(
        related_resource=digital_a,
        file_name="a.tif",
        s3_key="s3://bucket/a.tif",
        content_type="image/tiff",
        status="completed",
    )
    S3FileObject.objects.create(
        related_resource=digital_b,
        file_name="b.tif",
        s3_key="s3://bucket/b.tif",
        content_type="image/tiff",
        status="completed",
    )

    link_newest = base_ts + timedelta(days=12)
    link_b = OAIProjectMediaLink.objects.create(
        project=project,
        digital_object=digital_b,
        order_index=1,
        label_override="Curated Beta",
    )
    link_a = OAIProjectMediaLink.objects.create(
        project=project,
        digital_object=digital_a,
        order_index=2,
    )
    OAIProjectMediaLink.objects.filter(pk=link_b.pk).update(updated_at=link_newest)
    OAIProjectMediaLink.objects.filter(pk=link_a.pk).update(updated_at=base_ts + timedelta(days=5))

    publication = OAIProjectPublication.objects.create(project=project, is_approved=True)
    OAIProjectPublication.objects.filter(pk=publication.pk).update(updated_at=base_ts + timedelta(days=7))

    def _digital_payload(res: Resource, slug: str) -> ProjectDigitalObject:
        return ProjectDigitalObject(
            path=f"s3://bucket/{slug}.tif",
            storage_key=f"s3://bucket/{slug}.tif",
            file_name=f"{slug}.tif",
            content_type="image/tiff",
            storage_status="completed",
            source="s3",
            resource_id=str(res.id),
            uri=res.uri,
        )

    record = ProjectRecord(
        subject_id=str(project.id),
        uri=project.uri,
        title="Tailored Integration",
        institution=ProjectInstitution(label=org.name, code=org.code),
        digital_objects=[
            _digital_payload(digital_extra, "extra"),
        ],
        actors=[
            ProjectActor(name="Primary Creator", roles=["Creator"]),
            ProjectActor(name="Collab Mate", roles=["Collaborator"]),
        ],
        reference_project_uris=[
            "https://arkumu.org/entities/projekt/parent-1",
            "https://arkumu.org/entities/projekt/parent-2",
        ],
    )

    monkeypatch.setattr(
        project_views.project_builder, "_s3_orgs", set()
    )
    monkeypatch.setattr(project_views.project_builder, "_rosetta_orgs", set())
    monkeypatch.setattr(project_views.project_builder, "_path_resolver", lambda *args, **kwargs: [])
    monkeypatch.setattr(project_views._tailored_project_builder, "_s3_orgs", {org.code})
    monkeypatch.setattr(project_views._tailored_project_builder, "_rosetta_orgs", set())
    monkeypatch.setattr(project_views._tailored_project_builder, "_path_resolver", lambda *args, **kwargs: [])

    def _fake_find_fixity(org_code, candidates):
        for candidate in candidates:
            if candidate:
                return SimpleNamespace(
                    storage_key=candidate,
                    checksum_or_etag="sha256:cafebabe",
                    status="completed",
                )
        return None

    monkeypatch.setattr(
        "arkumu.projects.services.dump_fixity_index.find_fixity",
        _fake_find_fixity,
    )

    builder_check = project_views._tailored_project_builder.from_project_record(
        record,
        use_curated_media_links=True,
    )
    assert [obj.storage_key for obj in builder_check.digital_objects] == [
        "s3://bucket/b.tif",
        "s3://bucket/a.tif",
    ]

    monkeypatch.setattr(
        project_views,
        "_assemble_record_from_db",
        lambda resource: record if resource and resource.uri == project.uri else None,
    )
    monkeypatch.setattr(
        project_views,
        "snapshot_service",
        SimpleNamespace(
            get_record_by_uri=lambda uri: None,
            refresh_cross_institutional_snapshot=lambda: None,
        ),
    )
    monkeypatch.setattr(harvest_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(tailored_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(oai_views, "_project_type_filter", lambda: None)

    identifier = f"oai:arkumu:resource:{quote(project.uri)}"
    params = {"verb": "GetRecord", "identifier": identifier, "metadataPrefix": "mets"}
    headers = {"HTTP_X_INTERNAL_OAI_BYPASS": "1"}

    tailored_response = client.get("/oai/tailored/", params, **headers)
    db_response = client.get("/oai/db/", params, **headers)
    print("TAILORED", tailored_response.content.decode())

    assert tailored_response.status_code == 200
    assert db_response.status_code == 200
    ns = {
        "oai": "http://www.openarchives.org/OAI/2.0/",
        "mets": METS_NS,
        "xlink": "http://www.w3.org/1999/xlink",
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
        "xml": "http://www.w3.org/XML/1998/namespace",
    }

    def _record_root(response: object) -> ET._Element:
        document = ET.fromstring(response.content)
        record_elem = document.find(".//oai:GetRecord/oai:record", ns)
        if record_elem is None:
            assert False, ET.tostring(document, encoding="unicode")
        return record_elem

    def _file_hrefs(record_elem: ET._Element) -> list[str]:
        return [
            node.attrib[f"{{{ns['xlink']}}}href"]
            for node in record_elem.findall(".//mets:fileSec//mets:FLocat", ns)
        ]

    def _file_titles(record_elem: ET._Element) -> list[str]:
        titles: list[str] = []
        for node in record_elem.findall(".//mets:fileSec//mets:FLocat", ns):
            label = node.attrib.get(f"{{{ns['xlink']}}}title")
            if label:
                titles.append(label)
        return titles

    tailored_root = _record_root(tailored_response)
    db_root = _record_root(db_response)

    tailored_hrefs = _file_hrefs(tailored_root)
    db_hrefs = _file_hrefs(db_root)
    tailored_titles = _file_titles(tailored_root)
    db_titles = _file_titles(db_root)
    download_base = (
        getattr(settings, "AWS_S3_BROWSER_ENDPOINT_URL", "")
        or getattr(settings, "S3_HOSTNAME", "")
        or "http://localhost:9020"
    )
    if not download_base.startswith("http://") and not download_base.startswith("https://"):
        download_base = f"https://{download_base}"

    expected_tailored = [
        f"{download_base}/fuk/bucket/b.tif",
        f"{download_base}/fuk/bucket/a.tif",
    ]
    for href, expected in zip(tailored_hrefs, expected_tailored):
        assert expected in href

    assert len(db_hrefs) == 1
    assert "s3://bucket/extra.tif" in db_hrefs[0]

    assert tailored_titles[0] == "Curated Beta"
    assert "Curated Beta" not in db_titles

    tailored_datestamp = tailored_root.find("./oai:header/oai:datestamp", ns)
    db_datestamp = db_root.find("./oai:header/oai:datestamp", ns)
    assert tailored_datestamp is not None
    assert db_datestamp is not None
    expected_datestamp = link_newest.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert tailored_datestamp.text == expected_datestamp
    assert db_datestamp.text == base_ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    dc_record = tailored_root.find(".//mets:dmdSec/mets:mdWrap/mets:xmlData/dc:record", ns)
    assert dc_record is not None
    title_values = [elem.text for elem in dc_record.findall("./dc:title", ns) if elem.text]
    assert "Tailored Integration" in title_values

    creator_values = [elem.text for elem in dc_record.findall("./dc:creator", ns) if elem.text]
    assert "Primary Creator" in creator_values
    assert "Collab Mate" in creator_values

    contributor_values = [elem.text for elem in dc_record.findall("./dc:contributor", ns) if elem.text]
    assert any("Primary Creator" in value for value in contributor_values)
    assert any("Collab Mate" in value for value in contributor_values)

    part_of_values = [elem.text for elem in dc_record.findall("./dcterms:isPartOf", ns) if elem.text]
    assert part_of_values == ["https://arkumu.org/entities/projekt/parent-1"]


@pytest.mark.django_db
def test_tailored_endpoint_expands_khm_dcp_bundle_with_index(client, monkeypatch, settings):
    settings.OAI_BASIC_AUTH_ENABLED = False
    settings.OAI_ROSETTA_HARVESTABLE_ORGS = ("khm",)
    settings.OAI_S3_HARVESTABLE_ORGS = ()

    rosetta_root = "/rosetta/khm/sandbox/input/arkumu/daten"
    settings.OAI_EXTERNAL_ROSETTA_ROOTS = {"khm": rosetta_root}

    user = User.objects.create_user(username="oai-admin-dcp", password="test", role="system_admin")
    client.force_login(user)

    org = Organization.objects.create(name="KHM", code="khm", is_active=True)
    base_ts = timezone.now()
    project = Resource.objects.create(
        uri="https://arkumu.org/entities/projekt/9999",
        organization=org,
        resource_type=ResourceType.ENTITY,
        public_access_level=PublicAccessLevel.PUBLIC,
        is_public_approved=True,
    )
    Resource.objects.filter(pk=project.pk).update(updated_at=base_ts)

    # Digital object resource that will act as the DCP bundle anchor
    digital = Resource.objects.create(
        uri="https://arkumu.org/entities/digital/9999_dcp",
        organization=org,
        resource_type=ResourceType.ENTITY,
        public_access_level=PublicAccessLevel.PUBLIC,
        is_public_approved=True,
    )

    # DCP folder triple: use the dedicated KHM predicate with an absolute folder path
    dcp_predicate_uri = "http://arkumu.org/data/khm/properties/dateipfad-dcp-ordner"
    dcp_predicate = Resource.objects.create(
        uri=dcp_predicate_uri,
        canonical_uri=dcp_predicate_uri,
        resource_type=ResourceType.PROPERTY,
    )
    folder_name = "9999_bundle_dcp"
    folder_path = f"{rosetta_root}/{folder_name}"
    folder_literal = Resource.objects.create(
        value=folder_path,
        resource_type=ResourceType.LITERAL,
    )
    Triple.objects.create(subject=digital, predicate=dcp_predicate, object=folder_literal)

    # Curated media link and publication so the project is harvestable in tailored mode
    link = OAIProjectMediaLink.objects.create(
        project=project,
        digital_object=digital,
        order_index=1,
    )
    publication = OAIProjectPublication.objects.create(project=project, is_approved=True)
    OAIProjectMediaLink.objects.filter(pk=link.pk).update(updated_at=base_ts)
    OAIProjectPublication.objects.filter(pk=publication.pk).update(updated_at=base_ts + timedelta(seconds=1))

    # Seed the DCP path index directly for this bundle (relative to the Rosetta root)
    OAIDcpPathIndex.objects.create(
        org_code="khm",
        bundle_key=folder_name,
        folder_name=folder_name,
        relative_file_path=f"{folder_name}/feature.mxf",
        file_name="feature.mxf",
    )
    OAIDcpPathIndex.objects.create(
        org_code="khm",
        bundle_key=folder_name,
        folder_name=folder_name,
        relative_file_path=f"{folder_name}/audio.wav",
        file_name="audio.wav",
    )

    # Ensure tailored builder treats KHM as a Rosetta org and uses a simple path resolver
    monkeypatch.setattr(project_views._tailored_project_builder, "_s3_orgs", set())
    monkeypatch.setattr(project_views._tailored_project_builder, "_rosetta_orgs", {"khm"})

    def _fake_resolver(org_code, *, path=None, file_name=None):
        candidate = path or file_name
        return [candidate] if candidate else []

    monkeypatch.setattr(project_views._tailored_project_builder, "_path_resolver", _fake_resolver)

    # Disable project-type filters so the synthetic project is visible
    monkeypatch.setattr(harvest_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(tailored_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(oai_views, "_project_type_filter", lambda: None)

    # Use the real tailored builder on a minimal record via a stubbed project hint helper
    record = ProjectRecord(
        subject_id=str(project.id),
        uri=project.uri,
        title="KHM DCP integration",
        institution=ProjectInstitution(label=org.name, code=org.code),
        digital_objects=[],
    )

    def _build_hint(resource: Resource):
        assert resource.uri == project.uri
        builder = project_views._tailored_project_builder
        return builder.from_project_record(
            record,
            skip_shared_event_filter=False,
            skip_format_exclusion=False,
            use_curated_media_links=True,
        )

    monkeypatch.setattr(tailored_views, "_build_tailored_project_hint_from_resource", _build_hint)

    identifier = f"oai:arkumu:resource:{quote(project.uri)}"
    params = {"verb": "GetRecord", "identifier": identifier, "metadataPrefix": "mets"}
    headers = {"HTTP_X_INTERNAL_OAI_BYPASS": "1"}

    response = client.get("/oai/tailored/", params, **headers)
    assert response.status_code == 200

    ns = {
        "oai": "http://www.openarchives.org/OAI/2.0/",
        "mets": METS_NS,
        "xlink": "http://www.w3.org/1999/xlink",
    }

    document = ET.fromstring(response.content)
    record_elem = document.find(".//oai:GetRecord/oai:record", ns)
    if record_elem is None:
        assert False, ET.tostring(document, encoding="unicode")

    hrefs = {
        node.attrib[f"{{{ns['xlink']}}}href"]
        for node in record_elem.findall(".//mets:fileSec//mets:FLocat", ns)
    }

    expected_paths = {
        f"{rosetta_root}/{folder_name}/feature.mxf",
        f"{rosetta_root}/{folder_name}/audio.wav",
    }
    assert expected_paths.issubset(hrefs), hrefs


@pytest.mark.django_db
def test_tailored_resumption_tokens_include_profile_and_invalidate_on_dataset_change(client, monkeypatch, settings):
    settings.OAI_BASIC_AUTH_ENABLED = False

    user = User.objects.create_user(username="oai-admin", password="test", role="system_admin")
    client.force_login(user)

    monkeypatch.setattr(base_views.tailored_resumption_service, "page_size", 1)
    monkeypatch.setattr(harvest_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(tailored_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(oai_views, "_project_type_filter", lambda: None)
    monkeypatch.setattr(
        tailored_views,
        "_build_tailored_project_hint_from_resource",
        lambda resource: SimpleNamespace(harvestable=True, uri=resource.uri),
    )

    org = Organization.objects.create(name="Tailored Org", code="tailor", is_active=True)
    base_ts = timezone.now() - timedelta(days=5)

    def _make_project(slug: str, delta: int) -> Resource:
        project = Resource.objects.create(
            uri=f"https://arkumu.org/entities/projekt/{slug}",
            organization=org,
            resource_type=ResourceType.ENTITY,
            public_access_level=PublicAccessLevel.PUBLIC,
            is_public_approved=True,
        )
        Resource.objects.filter(pk=project.pk).update(updated_at=base_ts + timedelta(days=delta))
        publication = OAIProjectPublication.objects.create(project=project, is_approved=True)
        OAIProjectPublication.objects.filter(pk=publication.pk).update(updated_at=base_ts + timedelta(days=delta))

        digital = Resource.objects.create(
            uri=f"https://arkumu.org/entities/digital/{slug}",
            organization=org,
            resource_type=ResourceType.ENTITY,
            public_access_level=PublicAccessLevel.PUBLIC,
            is_public_approved=True,
        )
        link = OAIProjectMediaLink.objects.create(
            project=project,
            digital_object=digital,
        )
        OAIProjectMediaLink.objects.filter(pk=link.pk).update(updated_at=base_ts + timedelta(days=delta))
        return project

    projects = [_make_project("101", 1), _make_project("102", 2)]

    headers = {"HTTP_X_INTERNAL_OAI_BYPASS": "1"}
    params = {"verb": "ListIdentifiers", "metadataPrefix": "oai_dc"}
    ns = {"oai": "http://www.openarchives.org/OAI/2.0/"}

    response = client.get("/oai/tailored/", params, **headers)
    assert response.status_code == 200
    document = ET.fromstring(response.content)
    headers_nodes = document.findall(".//oai:ListIdentifiers/oai:header", ns)
    assert len(headers_nodes) == 1
    resumption_elem = document.find(".//oai:resumptionToken", ns)
    assert resumption_elem is not None and resumption_elem.text

    first_token = resumption_elem.text
    valid, payload, error = base_views.tailored_resumption_service.parse_token(first_token)
    assert valid
    assert payload["profile"] == "tailored"

    second_page = client.get(
        "/oai/tailored/",
        {"verb": "ListIdentifiers", "resumptionToken": first_token},
        **headers,
    )
    assert second_page.status_code == 200
    document_page2 = ET.fromstring(second_page.content)
    assert document_page2.find(".//oai:resumptionToken", ns) is None

    fresh_response = client.get("/oai/tailored/", params, **headers)
    fresh_token_elem = ET.fromstring(fresh_response.content).find(".//oai:resumptionToken", ns)
    assert fresh_token_elem is not None and fresh_token_elem.text
    stale_token = fresh_token_elem.text

    OAIProjectPublication.objects.filter(project=projects[-1]).update(updated_at=timezone.now())

    stale_response = client.get(
        "/oai/tailored/",
        {"verb": "ListIdentifiers", "resumptionToken": stale_token},
        **headers,
    )
    stale_document = ET.fromstring(stale_response.content)
    error_elem = stale_document.find(".//oai:error", ns)
    assert error_elem is not None
    assert error_elem.attrib.get("code") == "badResumptionToken"
    assert "Dataset has changed" in (error_elem.text or "")


@pytest.mark.django_db
def test_tailored_token_rejected_by_db_endpoint(client, settings):
    settings.OAI_BASIC_AUTH_ENABLED = False

    user = User.objects.create_user(username="oai-admin", password="test", role="system_admin")
    client.force_login(user)

    token = base_views.tailored_resumption_service.create_token(
        offset=0,
        verb="ListIdentifiers",
        metadata_prefix="oai_dc",
        cursor_marker="db-rejection",
    )

    response = client.get(
        "/oai/db/",
        {"verb": "ListIdentifiers", "resumptionToken": token},
        HTTP_X_INTERNAL_OAI_BYPASS="1",
    )

    ns = {"oai": "http://www.openarchives.org/OAI/2.0/"}
    document = ET.fromstring(response.content)
    error_elem = document.find(".//oai:error", ns)
    assert error_elem is not None
    assert error_elem.attrib.get("code") == "badResumptionToken"
    assert "profile" in (error_elem.text or "")


@pytest.mark.django_db
def test_tailored_builder_uses_curated_rosetta_paths(monkeypatch, settings):
    settings.OAI_BASIC_AUTH_ENABLED = False
    settings.OAI_S3_HARVESTABLE_ORGS = ()
    settings.OAI_ROSETTA_HARVESTABLE_ORGS = ("hmt",)
    settings.OAI_ROSETTA_CURATED_PREFIXES = {}
    settings.OAI_EXTERNAL_PATH_PREFIXES = {"hmt": ["/rosetta/hfmt"]}

    org = Organization.objects.create(name="HMT", code="hmt", is_active=True)
    project = Resource.objects.create(
        uri="https://arkumu.org/entities/projekt/300",
        organization=org,
        resource_type=ResourceType.ENTITY,
        public_access_level=PublicAccessLevel.PUBLIC,
        is_public_approved=True,
    )
    digital = Resource.objects.create(
        uri="https://arkumu.org/entities/digital/300",
        organization=org,
        resource_type=ResourceType.ENTITY,
        public_access_level=PublicAccessLevel.PUBLIC,
        is_public_approved=True,
    )

    OAIProjectMediaLink.objects.create(project=project, digital_object=digital)

    record = ProjectRecord(
        subject_id=str(project.id),
        uri=project.uri,
        title="Curated Rosetta",
        institution=ProjectInstitution(label=org.name, code=org.code),
        digital_objects=[
            ProjectDigitalObject(
                path="/Volumes/18TB1/hfmt_tonbandarchiv_dateien/TA072_b_002.mp3",
                file_name="TA072_b_002.mp3",
                resource_id=str(digital.id),
                storage_status="completed",
            )
        ],
    )

    builder = OAIProjectBuilderTailored()
    monkeypatch.setattr(builder, "_path_resolver", lambda *args, **kwargs: ())

    project_hint = builder.from_project_record(record, use_curated_media_links=True)
    assert len(project_hint.digital_objects) == 1
    normalized = project_hint.digital_objects[0]
    assert normalized.rosetta_path == "/rosetta/hfmt/TA072_b_002.mp3"
    assert normalized.harvestable
