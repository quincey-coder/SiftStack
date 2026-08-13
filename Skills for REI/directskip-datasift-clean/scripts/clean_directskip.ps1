# Clean the DirectSkip vendor return into a single DataSift-upload-ready CSV.
# Standalone: reads ONLY the vendor file. No merge, no second source.
#
# Column names are the verified auto-mapping labels from
# src/datasift_formatter.py DATASIFT_COLUMNS (phone slots extended to DataSift's
# full 30, which is what its own export format carries).
#
# Decisions in force:
#   * EVERY number is callable - owner, co-owner AND relatives all get real
#     dial slots. Notes carries a slot map saying who each number belongs to.
#   * mailing address stays as filed; vendor's confirmed address -> Notes only
#   * dial order = owner > co-owners > relatives; Mobile before Landline within
#     each person, so the best contact is always Phone 1.

$ErrorActionPreference = 'Stop'

$SkipFile = "C:\Users\Q\Downloads\248923-contactinfo-priority_1_196_record_second_skip_csv.csv"
$OutDir   = "C:\Users\Q\SiftStack\output\second_skip_08_2026"
$OutFile  = Join-Path $OutDir "skip_trace_CLEANED_08-2026.csv"
$Stamp    = "08/2026"
$MaxPhone = 30      # DataSift's ceiling - its own export carries Phone 1-30
$MaxEmail = 6

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }

# ── helpers ───────────────────────────────────────────────────────────────

function Get-Digits {
    param($Phone)
    $d = "$Phone" -replace '\D', ''
    if ($d.Length -eq 11 -and $d.StartsWith('1')) { $d = $d.Substring(1) }
    if ($d.Length -ne 10) { return '' }
    return $d
}

function Format-Phone {
    param($Digits)
    if ("$Digits".Length -ne 10) { return "$Digits" }
    return $Digits.Substring(0,3) + '-' + $Digits.Substring(3,3) + '-' + $Digits.Substring(6,4)
}

function Get-Zip5 {
    param($Zip)
    $z = "$Zip".Trim()
    if ($z -match '^(\d{5})') { return $matches[1] }
    return ''
}

function Get-TypeMark {
    param($VendorType)
    switch -Regex ("$VendorType".Trim()) {
        '^(?i)mobile'      { return 'M' }
        '^(?i)residential' { return 'L' }
        '^(?i)landline'    { return 'L' }
        default            { return 'O' }
    }
}

function Get-TypeRank {
    param($VendorType)
    switch -Regex ("$VendorType".Trim()) {
        '^(?i)mobile'      { return 0 }
        '^(?i)residential' { return 1 }
        '^(?i)landline'    { return 1 }
        default            { return 2 }
    }
}

# Vendor names arrive ALL-CAPS. CLAUDE.md treats ALL-CAPS as a corruption red
# flag, so everything gets title-cased. Never reorders the parts (no NAMELF flip).
function Format-Name {
    param($Name)
    $n = "$Name".Trim()
    if ($n -eq '') { return '' }
    $out = @()
    foreach ($w in ($n -split '\s+')) {
        if ($w.Length -le 1) { $out += $w.ToUpper(); continue }
        if ($w -match '^(?i)(II|III|IV|JR|SR|LLC|INC|MD|DDS)\.?$') { $out += $w.ToUpper(); continue }
        if ($w -match "^(?i)(Mc)(.+)$") {
            $out += 'Mc' + $matches[2].Substring(0,1).ToUpper() + $matches[2].Substring(1).ToLower(); continue
        }
        $out += ($w.Substring(0,1).ToUpper() + $w.Substring(1).ToLower())
    }
    return ($out -join ' ')
}

function Format-Addr {
    param($Street, $City, $State, $Zip)
    $parts = @()
    if ("$Street".Trim() -ne '') { $parts += (Format-Name $Street) }
    $cs = @()
    if ("$City".Trim()  -ne '') { $cs += (Format-Name $City) }
    if ("$State".Trim() -ne '') { $cs += "$State".ToUpper() }
    $z = Get-Zip5 $Zip
    if ($z -ne '') { $cs += $z }
    if ($cs.Count -gt 0) { $parts += ($cs -join ' ') }
    return ($parts -join ', ')
}

# ── header ────────────────────────────────────────────────────────────────

$cols = @(
    'Property Street Address', 'Property City', 'Property State', 'Property ZIP Code',
    'Owner First Name', 'Owner Last Name',
    'Mailing Street Address', 'Mailing City', 'Mailing State', 'Mailing ZIP Code'
)
for ($i = 1; $i -le $MaxPhone; $i++) { $cols += "Phone $i" }
for ($i = 1; $i -le $MaxEmail; $i++) { $cols += "Email $i" }
$cols += @('Tags', 'Notes', 'Owner Deceased')

# ── build ─────────────────────────────────────────────────────────────────

Write-Host "Loading $(Split-Path $SkipFile -Leaf)..."
$skip = Import-Csv $SkipFile

$rows = @()
$statPhones = 0; $statOverflowRecs = 0; $statOverflowNums = 0; $statEmails = 0
$statDeceased = 0; $statSuspect = 0; $statAddrDiff = 0; $statNoPhone = 0

foreach ($s in $skip) {

    $row = [ordered]@{}
    foreach ($c in $cols) { $row[$c] = '' }

    # ── property (the anchor DataSift matches on)
    $row['Property Street Address'] = Format-Name $s.'Input Property Address'
    $row['Property City']           = Format-Name $s.'Input Property City'
    $row['Property State']          = "$($s.'Input Property State')".ToUpper()
    $row['Property ZIP Code']       = Get-Zip5 $s.'Input Property Zip'

    # ── owner: prefer the vendor's matched name, fall back to what we sent.
    #    Both arrive already split into first/last, so there is no NAMELF flip
    #    risk here - only case to fix.
    $mf = Format-Name $s.'Matched First Name'
    $ml = Format-Name $s.'Matched Last Name'
    if ($mf -eq '' -and $ml -eq '') {
        $mf = Format-Name $s.'Input First Name'
        $ml = Format-Name $s.'Input Last Name'
    }
    $row['Owner First Name'] = $mf
    $row['Owner Last Name']  = $ml

    # ── mailing stays as filed; the vendor's confirmed address goes to Notes
    $row['Mailing Street Address'] = Format-Name $s.'Input Mailing Address'
    $row['Mailing City']           = Format-Name $s.'Input Mailing City'
    $row['Mailing State']          = "$($s.'Input Mailing State')".ToUpper()
    $row['Mailing ZIP Code']       = Get-Zip5 $s.'Input Mailing Zip'

    $isDeceased = ("$($s.Deceased)".Trim().ToUpper() -eq 'Y')
    $rc = "$($s.ResultCode)".Trim().ToUpper()
    $isSuspect = ($rc -ne 'CI')
    if ($isDeceased) { $row['Owner Deceased'] = 'yes'; $statDeceased++ }
    if ($isSuspect)  { $statSuspect++ }

    # ── collect every person on the record, in dial priority order.
    #    Owner first (best contact), then additional owners/residents at the
    #    property, then relatives. Everyone gets real dial slots.
    $people = @()

    $ownerLabel = 'OWNER'
    if ($isDeceased) { $ownerLabel = 'OWNER (DECEASED)' }
    $people += [PSCustomObject]@{
        Label = $ownerLabel
        Name  = "$mf $ml".Trim()
        Age   = "$($s.Age)".Trim()
        Pre   = ''
        Kind  = 'owner'
    }

    foreach ($pi in @(2, 3)) {
        $pre = "Person$pi "
        $pn = (Format-Name "$($s."${pre}First Name") $($s."${pre}Last Name")").Trim()
        if ($pn -eq '') { continue }
        $lbl = "PERSON $pi (additional owner/resident)"
        if ("$($s."${pre}Deceased")".Trim().ToUpper() -eq 'Y') { $lbl += ' [DECEASED]' }
        $people += [PSCustomObject]@{
            Label = $lbl; Name = $pn; Age = "$($s."${pre}Age")".Trim(); Pre = $pre; Kind = 'person'
        }
    }

    foreach ($pi in @(0, 2, 3)) {
        if ($pi -eq 0) { $pre = '' } else { $pre = "Person$pi " }
        for ($j = 1; $j -le 5; $j++) {
            $rn = "$($s."${pre}Relative$j Name")".Trim()
            if ($rn -eq '') { continue }
            if ($pi -eq 0) {
                if ($isDeceased) { $lbl = "HEIR CANDIDATE $j (relative of deceased owner)" }
                else             { $lbl = "RELATIVE $j (of owner)" }
            } else {
                $lbl = "RELATIVE $j (of Person $pi)"
            }
            $people += [PSCustomObject]@{
                Label = $lbl; Name = (Format-Name $rn); Age = "$($s."${pre}Relative$j Age")".Trim()
                Pre = $pre; Kind = 'relative'; RelIdx = $j
            }
        }
    }

    # ── pull each person's phones, Mobile before Landline within the person
    foreach ($p in $people) {
        $ph = @()
        if ($p.Kind -eq 'relative') {
            for ($k = 1; $k -le 5; $k++) {
                $d = Get-Digits $s."$($p.Pre)Relative$($p.RelIdx) Phone$k"
                if ($d -eq '') { continue }
                $vt = $s."$($p.Pre)Relative$($p.RelIdx) Phone$k Type"
                $ph += [PSCustomObject]@{ Digits = $d; Rank = (Get-TypeRank $vt); Mark = (Get-TypeMark $vt) }
            }
        } else {
            for ($k = 1; $k -le 7; $k++) {
                $d = Get-Digits $s."$($p.Pre)Phone$k"
                if ($d -eq '') { continue }
                $vt = $s."$($p.Pre)Phone$k Type"
                $ph += [PSCustomObject]@{ Digits = $d; Rank = (Get-TypeRank $vt); Mark = (Get-TypeMark $vt) }
            }
        }
        $p | Add-Member -NotePropertyName Phones -NotePropertyValue ($ph | Sort-Object Rank)
    }

    # ── assign slots. A number shared by two people (common - a co-owner also
    #    shows up as a relative) occupies ONE slot and is reported under both.
    $slotOf = @{}
    $slot = 0
    $overflow = @()
    foreach ($p in $people) {
        foreach ($ph in $p.Phones) {
            if ($slotOf.ContainsKey($ph.Digits)) { continue }
            if ($slot -ge $MaxPhone) {
                if ($overflow -notcontains $ph.Digits) { $overflow += $ph.Digits }
                continue
            }
            $slot++
            $slotOf[$ph.Digits] = $slot
            $row["Phone $slot"] = $ph.Digits
            $statPhones++
        }
    }
    if ($slot -eq 0) { $statNoPhone++ }
    if ($overflow.Count -gt 0) { $statOverflowRecs++; $statOverflowNums += $overflow.Count }

    # ── emails
    $eseen = New-Object 'System.Collections.Generic.HashSet[string]'
    $eslot = 0
    foreach ($pre in @('', 'Person2 ', 'Person3 ')) {
        foreach ($i in @(1, 2)) {
            $v = "$($s."${pre}Email$i")".Trim().ToLower()
            if ($v -eq '') { continue }
            if (-not $eseen.Add($v)) { continue }
            if ($eslot -ge $MaxEmail) { continue }
            $eslot++
            $row["Email $eslot"] = $v
            $statEmails++
        }
    }

    # ── Notes: the slot map is the whole point - who each number belongs to
    $n = @()
    $n += "=== DIRECTSKIP SKIP TRACE - $Stamp ==="
    $hdr = @()
    $matchedName = "$mf $ml".Trim()
    if ($matchedName -ne '') { $hdr += "Matched: $matchedName" }
    if ("$($s.Age)".Trim() -ne '') { $hdr += "age $($s.Age)" }
    if ($rc -ne '') { $hdr += "result $rc" }
    if ($hdr.Count -gt 0) { $n += ($hdr -join ' | ') }

    if ($isDeceased) {
        $n += ''
        $n += '** OWNER REPORTED DECEASED - the decision maker is an heir below, not the owner. **'
    }
    if ($isSuspect) {
        $n += ''
        if ("$($s.'Matched First Name')".Trim() -eq '' -and "$($s.'Matched Last Name')".Trim() -eq '') {
            $n += '** NO MATCH RETURNED - the vendor found no person for this record.'
            $n += "   Input name: $(Format-Name "$($s.'Input First Name') $($s.'Input Last Name')")"
            $n += '   Nothing was added. Re-skip with a corrected owner name. **'
        } else {
            $n += "** LOW-CONFIDENCE MATCH ($rc) - vendor matched on address, not name."
            $n += "   Input name: $(Format-Name "$($s.'Input First Name') $($s.'Input Last Name')") / Returned: $matchedName"
            $n += '   Verify identity before dialing. **'
        }
    }

    $confAddr = Format-Addr $s.'Confirmed Mailing Address' $s.'Confirmed Mailing City' $s.'Confirmed Mailing State' $s.'Confirmed Mailing Zip'
    $confDiffers = $false
    if ($confAddr -ne '') {
        $ca = ("$($s.'Confirmed Mailing Address')" -replace '[^A-Za-z0-9]', '').ToUpper()
        $la = ("$($s.'Input Mailing Address')"     -replace '[^A-Za-z0-9]', '').ToUpper()
        if ($ca -ne $la) {
            $confDiffers = $true; $statAddrDiff++
            $n += ''
            $n += 'CONFIRMED MAILING ADDRESS (skip trace - NOT applied to record):'
            $n += "  $confAddr"
            $n += "  on file: $(Format-Addr $s.'Input Mailing Address' $s.'Input Mailing City' $s.'Input Mailing State' $s.'Input Mailing Zip')"
        }
    }

    # Keyed by the NUMBER, not by slot position: DataSift appends these behind
    # whatever phones the record already had, so any slot number we printed here
    # would point at the wrong row. Look up the number you are dialing.
    $n += ''
    $n += '--- WHO EACH NUMBER BELONGS TO (dial reference) ---'
    $n += 'Look up the number you are calling. Order below = dial priority.'
    $overflowByPerson = @()
    foreach ($p in $people) {
        if ($p.Phones.Count -eq 0) { continue }
        $who = $p.Label + ': ' + $p.Name
        if ($p.Age -ne '') { $who += ", age $($p.Age)" }
        $n += ''
        $n += $who
        foreach ($ph in $p.Phones) {
            if ($slotOf.ContainsKey($ph.Digits)) {
                $n += ('  {0} ({1})' -f (Format-Phone $ph.Digits), $ph.Mark)
            } else {
                $n += ('  {0} ({1})  <- OVERFLOW, not uploaded, dial manually' -f (Format-Phone $ph.Digits), $ph.Mark)
                $overflowByPerson += ('  {0} ({1})  -  {2}: {3}' -f (Format-Phone $ph.Digits), $ph.Mark, $p.Label, $p.Name)
            }
        }
    }

    if ($overflow.Count -gt 0) {
        $n += ''
        $n += "=== OVERFLOW NUMBERS - $($overflow.Count) NOT UPLOADED ==="
        $n += "This record found $($slotOf.Count + $overflow.Count) numbers but DataSift only holds 30."
        $n += 'The numbers below have NO phone slot. They are recorded here only -'
        $n += 'dial them manually from this list.'
        $n += ''
        $n += ($overflowByPerson | Select-Object -Unique)
    }

    $n += ''
    $n += "Numbers uploaded: $slot of $($slotOf.Count + $overflow.Count) found.  M=mobile L=landline O=other"
    $row['Notes'] = ($n -join "`n")

    # ── tags
    $tags = @('skip2', 'DirectSkip', "Second Skip $Stamp")
    if ($isDeceased) { $tags += 'skip2 deceased' } else { $tags += 'living' }
    if ($isSuspect)   { $tags += 'skip2 low confidence match' }
    if ($confDiffers) { $tags += 'skip2 confirmed addr differs' }
    if ($slot -eq 0)  { $tags += 'skip2 no phone' }
    if ($overflow.Count -gt 0) { $tags += 'skip2 phone overflow' }
    $row['Tags'] = ($tags -join ',')

    $rows += [PSCustomObject]$row
}

$rows | Select-Object $cols | Export-Csv -Path $OutFile -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Wrote $($rows.Count) rows -> $(Split-Path $OutFile -Leaf)"
Write-Host "  columns                : $($cols.Count)  (Phone 1-$MaxPhone, Email 1-$MaxEmail)"
Write-Host "  phones uploaded        : $statPhones"
Write-Host "  emails uploaded        : $statEmails"
Write-Host "  records over 30 slots  : $statOverflowRecs  ($statOverflowNums numbers not uploaded, kept in Notes)"
Write-Host "  deceased flagged       : $statDeceased"
Write-Host "  low-confidence rows    : $statSuspect"
Write-Host "  confirmed addr differs : $statAddrDiff"
Write-Host "  rows with NO phone     : $statNoPhone"
