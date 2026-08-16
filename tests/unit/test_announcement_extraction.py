from __future__ import annotations

from dataclasses import asdict, fields

from ncs_jd.application.announcement_extraction import extract_announcement
from ncs_jd.application.document_parser import (
    DocumentMetadata,
    ParseQualitySummary,
    ParsedBlock,
    ParsedDocument,
    SourceLocator,
)


def _document(*blocks: ParsedBlock, markdown: str = "") -> ParsedDocument:
    return ParsedDocument(
        source_name="채용공고.pdf",
        document_format="pdf",
        markdown=markdown or "\n".join(block.markdown for block in blocks),
        blocks=tuple(blocks),
        metadata=DocumentMetadata(page_count=3),
        quality=ParseQualitySummary(
            total_blocks=len(blocks),
            text_blocks=len(blocks),
            table_blocks=sum(block.block_type == "table" for block in blocks),
            page_count=3,
            character_count=sum(len(block.markdown) for block in blocks),
            warning_count=0,
            status="good" if blocks else "empty",
        ),
    )


def _block(
    index: int,
    block_type: str,
    *,
    text: str = "",
    markdown: str = "",
    rows: tuple[tuple[str, ...], ...] = (),
    page: int = 1,
) -> ParsedBlock:
    return ParsedBlock(
        locator=SourceLocator(f"block-{index + 1:04d}", index, page),
        block_type=block_type,  # type: ignore[arg-type]
        text=text,
        markdown=markdown,
        table_rows=rows,
    )


def test_multi_role_table_is_split_deterministically() -> None:
    document = _document(
        _block(
            0,
            "table",
            page=2,
            rows=(
                ("채용분야", "담당업무", "지원자격", "우대사항", "NCS 세분류"),
                ("인사", "채용계획 수립\n면접 운영", "관련 분야 경력", "자격증 소지자", "인사·조직"),
                ("홍보", "보도자료 작성", "학력 제한 없음", "공공기관 경험", "PR"),
            ),
        )
    )

    first = extract_announcement(document)
    second = extract_announcement(document)

    assert first == second
    assert [candidate.candidate_id for candidate in first.role_candidates] == ["role-001", "role-002"]
    assert [candidate.role_title.text for candidate in first.role_candidates if candidate.role_title] == ["인사", "홍보"]
    assert [item.text for item in first.role_candidates[0].duties] == ["채용계획 수립", "면접 운영"]
    assert first.role_candidates[0].ncs_subcategory_candidates[0].text == "인사·조직"
    assert all(item.source_locator.block_id == "block-0001" for item in first.role_candidates[0].duties)
    assert all(item.source_locator.page_number == 2 for item in first.role_candidates[0].duties)
    assert "multiple_roles_review_required" in {flag.code for flag in first.review_flags}


def test_split_heading_and_markdown_only_table_extracts_role_and_duties() -> None:
    document = _document(
        _block(0, "heading", text="채용 분야", markdown="### 채용 분야"),
        _block(1, "heading", text="및 담당 업무", markdown="### 및 담당 업무"),
        _block(
            2,
            "table",
            markdown=(
                "| 직종 | 직급 | 채용인원 | 담당 업무 | 채용예정일 |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| 기능직<br>(전기) | 5등급<br>(부기능원) | 1명 | "
                "◦ 전기 관련 설비 점검<br>◦ 전기시설물 유지 보수 업무 | 2026.01.01. |"
            ),
        ),
    )

    result = extract_announcement(document)

    assert len(result.role_candidates) == 1
    candidate = result.role_candidates[0]
    assert candidate.role_title is not None
    assert candidate.role_title.text == "기능직 (전기)"
    assert [item.text for item in candidate.duties] == [
        "전기 관련 설비 점검",
        "전기시설물 유지 보수 업무",
    ]
    assert "duties_missing" not in {flag.code for flag in result.review_flags}


def test_paragraph_sections_preserve_source_locator_and_review_requirement() -> None:
    document = _document(
        _block(0, "heading", text="채용분야: 기록물관리", markdown="## 채용분야: 기록물관리", page=1),
        _block(1, "heading", text="주요업무", markdown="## 주요업무", page=2),
        _block(2, "list", text="기록물 분류", markdown="- 기록물 분류\n- 이관 계획 수립", page=2),
        _block(3, "paragraph", text="NCS 세분류: 기록물관리", markdown="NCS 세분류: 기록물관리", page=3),
    )

    result = extract_announcement(document)
    candidate = result.role_candidates[0]

    assert candidate.role_title is not None
    assert candidate.role_title.text == "기록물관리"
    assert [item.text for item in candidate.duties] == ["기록물 분류", "이관 계획 수립"]
    assert candidate.duties[0].source_locator == SourceLocator("block-0003", 2, 2)
    assert candidate.duties[0].extraction_method == "heading_section"
    assert candidate.duties[0].review_required is True
    assert candidate.ncs_subcategory_candidates[0].source_locator.page_number == 3


def test_pdf_style_qualification_and_preference_sections_stop_at_numbered_heading() -> None:
    text = """응시 자격 요건
◦ 전기 분야 기능사 이상의 자격증 소지자
가점 및 우대 사항
◦ 장애인
3
근무 조건
◦ 주 5일 근무
"""
    result = extract_announcement(
        _document(_block(0, "paragraph", text=text, markdown=text))
    )

    candidate = result.role_candidates[0]
    assert [item.text for item in candidate.qualifications] == [
        "전기 분야 기능사 이상의 자격증 소지자"
    ]
    assert [item.text for item in candidate.preferences] == ["장애인"]


def test_role_with_empty_duties_gets_review_flag() -> None:
    document = _document(
        _block(
            0,
            "table",
            rows=(("채용분야", "지원자격"), ("전산", "관련 자격증 소지자")),
        )
    )

    result = extract_announcement(document)

    assert len(result.role_candidates) == 1
    assert result.role_candidates[0].duties == ()
    assert any(
        flag.code == "duties_missing" and flag.role_candidate_id == "role-001"
        for flag in result.review_flags
    )


def test_attachment_reference_only_and_empty_document_are_flagged() -> None:
    attachment = _document(
        _block(0, "heading", text="담당업무", markdown="## 담당업무"),
        _block(1, "paragraph", text="붙임 참조", markdown="붙임 참조"),
    )
    empty = _document(markdown="")

    attachment_result = extract_announcement(attachment)
    empty_result = extract_announcement(empty)

    assert "attachment_reference_only" in {flag.code for flag in attachment_result.review_flags}
    assert "duties_missing" in {flag.code for flag in attachment_result.review_flags}
    assert empty_result.role_candidates == ()
    assert "announcement_empty" in {flag.code for flag in empty_result.review_flags}


def test_extraction_contract_has_no_automatic_approval_fields() -> None:
    document = _document(
        _block(0, "paragraph", text="채용분야: 인사", markdown="채용분야: 인사"),
        _block(1, "paragraph", text="담당업무: 채용 운영", markdown="담당업무: 채용 운영"),
    )
    result = extract_announcement(document)
    serialized = asdict(result)
    forbidden = {"reviewed", "accepted", "human_reviewed", "approved", "approval_status"}

    assert forbidden.isdisjoint({field.name for field in fields(type(result.role_candidates[0]))})
    assert not any(token in repr(serialized) for token in forbidden)
    assert result.role_candidates[0].duties[0].review_required is True


def test_pasted_posting_preserves_reason_qualifications_and_preferences() -> None:
    text = """채용 사유
구성원의 인권보호와 폭력 예방을 위한 상담·교육 실무 인력이 필요함.
담당 업무
- 고충상담활동
- 예방교육 및 홍보 업무
- 사건 조사 및 처리 업무
- 기타 소속부서에서 부여한 각종 업무
채용 조건
(자격 조건)
- 관련 분야 석사학위 취득자
- 관련 분야 학사학위 취득 후 2년 이상 관련 경력 있는 자
우대 사항
- 상담심리사 2급 이상 소지자
- 외국어(영어) 능통자
"""
    result = extract_announcement(
        _document(_block(0, "paragraph", text=text, markdown=text))
    )

    candidate = result.role_candidates[0]
    assert candidate.role_title is None
    assert [item.text for item in candidate.recruitment_reasons] == [
        "구성원의 인권보호와 폭력 예방을 위한 상담·교육 실무 인력이 필요함."
    ]
    assert [item.text for item in candidate.duties][-1] == "기타 소속부서에서 부여한 각종 업무"
    assert [item.text for item in candidate.qualifications] == [
        "관련 분야 석사학위 취득자",
        "관련 분야 학사학위 취득 후 2년 이상 관련 경력 있는 자",
    ]
    assert [item.text for item in candidate.preferences] == [
        "상담심리사 2급 이상 소지자",
        "외국어(영어) 능통자",
    ]
