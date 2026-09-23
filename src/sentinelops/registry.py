"""Serialization policy for models fitted by SentinelOps itself."""
# Do not extend this list automatically based on an externally supplied model.
SKOPS_TRUSTED_TYPES = ["sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor"]
