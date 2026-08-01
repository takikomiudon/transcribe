on run argv
    if (count argv) is not 1 then error "実行するコマンドを1つ指定してください。"

    set shellCommand to item 1 of argv
    set tabTitle to "Live Transcribe"
    set terminalWasRunning to application "Terminal" is running

    tell application "Terminal"
        set targetTab to missing value
        set targetWindow to missing value

        if terminalWasRunning then
            repeat with w in windows
                repeat with t in (get tabs of w)
                    if custom title of t is tabTitle then
                        set targetTab to t
                        set targetWindow to w
                        exit repeat
                    end if
                end repeat
                if targetTab is not missing value then exit repeat
            end repeat
        end if

        if targetTab is missing value then
            set targetTab to do script shellCommand
            set custom title of targetTab to tabTitle
            set title displays custom title of targetTab to true
        else
            set selected tab of targetWindow to targetTab
            set frontmost of targetWindow to true
            if busy of targetTab is false then
                do script shellCommand in targetTab
            end if
        end if

        activate
    end tell
end run
