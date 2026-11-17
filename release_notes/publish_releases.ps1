# Creates a GitHub Release for every version tag. Requires: gh CLI installed and `gh auth login` done.
$repo = "esgindy/OmniSync-Downloader-OSDD"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($t in "v1.0.0","v2.0.0","v3.0.0","v4.0.0","v5.0.0","v5.0.1","v6.0.0","v6.0.1") {
  $latest = if ($t -eq "v6.0.0","v6.0.1") { "--latest" } else { "--latest=false" }
  gh release create $t --repo $repo --title "OmniSync Downloader (OSDD) $t" --notes-file "$here\$t.md" $latest --verify-tag
}
