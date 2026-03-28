storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  // SECURITY WARNING: For local dev only. TLS must be enabled in production environments!
  tls_disable = 1
}

ui = true
disable_mlock = true
