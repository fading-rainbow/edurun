function Get-ExperimentalAvd {
    param([ValidateSet('api34','api36')][string]$Variant='api34')
    if ($Variant -eq 'api36') {
        return [pscustomobject]@{Name='BudaoRun_API36_Play';Port=5582;Ram=3072;Package='system-images;android-36;google_apis_playstore;x86_64'}
    }
    return [pscustomobject]@{Name='BudaoRun_API34_Play';Port=5580;Ram=2048;Package='system-images;android-34;google_apis_playstore;x86_64'}
}
