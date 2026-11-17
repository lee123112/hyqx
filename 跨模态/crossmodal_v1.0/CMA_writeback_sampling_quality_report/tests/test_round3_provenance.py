from utils.review import resolve_review, review_summary
from utils.spot_check import build_sampling_conclusion


def test_simulated_human_survives_review_normalization():
    review=resolve_review({'human_review': {'verdict':'passed','reviewer':'test-reviewer',
                                           'simulated_human':True}},require_reviewer=True)
    assert review_summary(review)['simulated_human'] is True


def test_independent_conclusion_labels_simulation():
    result=build_sampling_conclusion({'simulated_human':True}, {}, unique_id='test_f1',run_id='run001',
                                    sampling={},conclusions={},cancelled_ids=[],strata_detail={},
                                    writeback_complete=True,generated_at='2026-10-10',
                                    source_name='20_quality_report.json',source_available=True)
    assert result['simulated_human'] is True
