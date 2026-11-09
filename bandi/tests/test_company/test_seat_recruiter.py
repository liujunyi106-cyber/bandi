from __future__ import annotations

from bandi.company.seat_recruiter import Recruiter, RecruitStatus


class TestRecruiter:
    def test_register_seat_available(self) -> None:
        r = Recruiter()
        seat = r.register_seat("eng")
        assert seat.role_id == "eng"
        assert seat.status == RecruitStatus.AVAILABLE

    def test_recruit_transitions_available_to_recruited(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        seat = r.recruit("eng")
        assert seat is not None
        assert seat.status == RecruitStatus.RECRUITED

    def test_recruit_returns_none_for_unknown_role(self) -> None:
        r = Recruiter()
        assert r.recruit("unknown") is None

    def test_recruit_returns_none_when_busy(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        r.dispatch("eng")  # sets BUSY directly
        assert r.recruit("eng") is None

    def test_release_transitions_recruited_to_available(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        r.recruit("eng")
        assert r.release("eng") is True
        seat = r.get_seat("eng")
        assert seat is not None
        assert seat.status == RecruitStatus.AVAILABLE

    def test_release_returns_false_when_already_available(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        assert r.release("eng") is False

    def test_dispatch_recruits_and_buries(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        assert r.dispatch("eng") is True
        seat = r.get_seat("eng")
        assert seat is not None
        assert seat.status == RecruitStatus.BUSY

    def test_list_available_only_shows_available(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        r.register_seat("pm")
        r.recruit("eng")
        avail = r.list_available()
        assert len(avail) == 1
        assert avail[0].role_id == "pm"

    def test_list_seats_returns_all(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        r.register_seat("pm")
        assert len(r.list_seats()) == 2

    def test_recruit_then_release_then_recruit_again(self) -> None:
        r = Recruiter()
        r.register_seat("eng")
        assert r.recruit("eng") is not None
        assert r.release("eng") is True
        assert r.recruit("eng") is not None
