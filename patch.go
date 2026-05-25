package main

import (
	"crypto/rand"
	"encoding/hex"
)

func GenerateElasticPassword() (string, error) {
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return "flume_es_" + hex.EncodeToString(bytes), nil
}
