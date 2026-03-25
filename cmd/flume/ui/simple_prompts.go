package ui

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// PromptForPort requests an alternate port gracefully if a collision occurs natively.
func PromptForPort(port int) int {
	fmt.Print(WarningGold(fmt.Sprintf("⚠️ FIREWALL COLLISION: Port %d is actively bound! Enter an alternate bypass port (or 0 to abort): ", port)))
	reader := bufio.NewReader(os.Stdin)
	input, _ := reader.ReadString('\n')
	input = strings.TrimSpace(input)
	if input == "" {
		return port
	}
	p, err := strconv.Atoi(input)
	if err != nil {
		fmt.Println(ErrorRed("Invalid numeric port boundary. Aborting topology injection."))
		return 0
	}
	return p
}

// PromptForInstall requests consent to violently inject missing OS components globally.
func PromptForInstall(missing []string) bool {
	fmt.Print(WarningGold(fmt.Sprintf("\n⚠️ KERNEL ALERT: Missing native framework dependencies detected:\n %v\n\nExecute autonomous sequence? [Y/n]: ", strings.Join(missing, ", "))))
	reader := bufio.NewReader(os.Stdin)
	input, _ := reader.ReadString('\n')
	input = strings.TrimSpace(strings.ToLower(input))
	return input == "" || input == "y" || input == "yes"
}
