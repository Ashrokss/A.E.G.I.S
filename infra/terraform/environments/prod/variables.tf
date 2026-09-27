variable "aws_region" {
  description = "Region."
  type        = string
  default     = "us-east-1"
}

variable "account_id" {
  description = "Your AWS account id. The provider refuses to run against any other."
  type        = string
}

variable "image_tag" {
  description = "Image tag to deploy (git SHA). CI passes -var image_tag=<sha>."
  type        = string
}

variable "alarm_email" {
  description = "Where alarms and budget alerts go."
  type        = string
  default     = ""
}

variable "bedrock_model_chat" {
  description = "Chat model id, e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0. Blank = mock mode."
  type        = string
  default     = ""
}

variable "bedrock_model_codegen" {
  description = "Codegen / repair model id (a Sonnet-class inference profile). Blank = mock mode."
  type        = string
  default     = ""
}

variable "bedrock_embed_model" {
  description = "Embedding model id, e.g. amazon.titan-embed-text-v2:0. Blank = keyword-only KB."
  type        = string
  default     = ""
}
