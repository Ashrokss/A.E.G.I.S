output "detector_id" {
  description = "Detector id, or null when disabled."
  value       = one(aws_guardduty_detector.main[*].id)
}
