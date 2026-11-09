import pytest

from bandi.eval.fairness_evaluator import FairnessEvaluator


class TestFairnessEvaluator:
    @pytest.fixture
    def evaluator(self):
        return FairnessEvaluator()

    def test_clean_text_no_issues(self, evaluator):
        report = evaluator.evaluate("Hello, how can I help you today?")
        assert not report.has_issues
        assert report.score == 1.0

    def test_empty_text(self, evaluator):
        report = evaluator.evaluate("")
        assert not report.has_issues

    def test_detects_male_biased_term(self, evaluator):
        report = evaluator.evaluate("The chairman approved the proposal.")
        assert report.has_issues
        assert any(i.dimension == "gender" for i in report.issues)
        assert any(i.term == "chairman" for i in report.issues)
        assert any(i.suggestion == "chairperson" for i in report.issues)

    def test_detects_female_biased_term(self, evaluator):
        report = evaluator.evaluate("She works as an actress.")
        assert report.has_issues
        assert any(i.dimension == "gender" for i in report.issues)
        assert any(i.term == "actress" for i in report.issues)

    def test_detects_developed_centric_term(self, evaluator):
        report = evaluator.evaluate("First world countries should help.")
        assert report.has_issues
        assert any(i.dimension == "region" for i in report.issues)

    def test_detects_developing_stereotype(self, evaluator):
        report = evaluator.evaluate("That is so third world.")
        assert report.has_issues
        assert any(i.dimension == "region" for i in report.issues)
        high_severity = [i for i in report.issues if i.severity == "high"]
        assert len(high_severity) > 0

    def test_detects_ableist_vocabulary(self, evaluator):
        report = evaluator.evaluate("That idea is stupid.")
        assert report.has_issues
        assert any(i.dimension == "vocabulary" for i in report.issues)
        assert any(i.issue_type == "ableist" for i in report.issues)

    def test_detects_classist_vocabulary(self, evaluator):
        report = evaluator.evaluate("That is so ghetto.")
        assert report.has_issues
        assert any(i.dimension == "vocabulary" for i in report.issues)
        assert any(i.issue_type == "classist" for i in report.issues)

    def test_multiple_issues_lower_score(self, evaluator):
        report = evaluator.evaluate("The chairman is stupid. Third world country.")
        assert report.has_issues
        assert len(report.issues) >= 3
        assert report.score < 1.0
        assert report.score >= 0.0

    def test_evaluate_messages(self, evaluator):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "The chairman approved it"},
        ]
        reports = evaluator.evaluate_messages(msgs)
        assert len(reports) == 2
        assert not reports[0].has_issues
        assert reports[1].has_issues

    def test_summary_no_issues(self, evaluator):
        report = evaluator.evaluate("Hello")
        assert report.summary == "No fairness issues detected."

    def test_summary_with_issues(self, evaluator):
        report = evaluator.evaluate("chairman stupid")
        summary = report.summary
        assert "Fairness score" in summary
        assert "gender" in summary
        assert "vocabulary" in summary
