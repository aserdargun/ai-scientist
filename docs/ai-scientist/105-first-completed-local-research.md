# İlk terminal raporlu yerel model araştırması

2026-10-01. Koşu `9564b284-d6f8-4297-9f62-1a58383536bd`.
Arayüz **http://HOST:8788** → **Deneyler** → koşuyu aç → **Rapor**.
Mevcut console rapor endpoint'i HTTP200; SQL ve rapor hash'i eşleşti.

## Geçen

Gerçek yerel model → sandbox/bağımsız Scorer → deterministik Referee →
ayrı holdout → terminal rapor → fiziksel GPU cleanup yolu tamamlandı.
Director exit0; SQL `completed`; 1456,713 saniye (24 dakika17 saniye).
Dört **sentetik** geliştirme görevi, ayrı özel holdout görevleri; 36 baseline
ve4 aday olmak üzere40 SHA-doğrulanmış Scorer ölçümü,117 checkpoint.
Tek gerçek S1 önerisi **DISCARD**: delta−0,9278042444, CI alt sınır−1.
KEEP yok; model iyileşmesi kanıtlanmadı. Holdout'ta korunan robust-z baseline
`passed`; reddedilmiş model adayı holdout şampiyonu olarak sunulmaz.

| Ölçüm / ayar | Doğrulanmış değer |
|---|---|
| Admission kaynak | `be94b60990f9c57cb882d7e945608916e697d903` |
| Result-time kaynak | `fcec2c61f2ac85ae04886b67bc6a55c5c93e53ca` |
| Salt okunur AOS HEAD | `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` |
| Model | Qwen/Qwen3.5-9B |
| Model revision | `c202236235762e1c871ad0ccb60c8ee5ba337b9a` |
| Quantization | fp8_per_tensor |
| S1 context / output / model max | 8192 /2048 /10240 token |
| Gerçek input / output | 1460 /302 token |
| Immutable bütçe | 1 öneri /7200s /30000 model token |
| Startup / inference / drain | 76,446s /11,824s /1,238s |
| Tepe VRAM | 12888MiB (12,586GiB) |
| Model cgroup tepe RAM | 10242224128 byte |
| GPU tahsis token | 13; exact kayıtlı owner için request `done` |

Scheduler `active_owner=null`, `next_owner=aos`. Exact model ünitesi
inactive/MainPID0; original PID383316 ve GPU child383903, cgroup ve UDS yok.
Readback yalnız KWin12MiB process; toplam GPU46MiB. Bu kontrol modelin
kapanmasını kanıtlar; yalnız stop ACK veya AOS idle durumuna dayanmaz.

Canonical rapor SHA256:
`afef102d053e5bac1c39255ba62c4b6d0ccb899248268a5673f45178d98759ca`.
Private HTML SHA256:
`9a4270f792a89f41216f40c18fbff075591dd7287f5323c4e9b46e423a1f4e91`.
Private terminal kanıtı SHA256: `4e6f2875f0533cb59975b72c31e400b06ceed3eec02aa82dab63778760dcf01b`.

Ham checkpoint/trajectory/token ve özel holdout sayısal skorları yayımlanmadı.
Provider ölçümleri immutable receipt'ten SHA doğrulanarak alındı; scheduler
binding timing alanları null. Kuyruk bekleme ve AOS GPU devir gecikmesi bu
koşuda ayrı ölçülmedi; elapsed−startup hesabından türetilmez.

## Kalan / çalıştırılmayan

- M0.13'ün tek başarılı koşuda ≥6 öneri ve S1/S2 çeşitliliği, kapasite/egress.
- Public/industrial araştırma; gerçek AOS kontrollü görev/devir/rapor doğrulama
  ve adil birlikte ilerleme (M0.AOS.7). Bu koşu onların kabulü değildir.
- Yeni responsive stop observer'ın gerçek GPU inflight iptal/toparlanma kabulü.
- Yeni structured LSH/OPTICS/SOM model önerisi sözleşmesinin canlı araştırması.
- Kanıtlı sürekli iyileşme ve eğitim/adaptör kabulü. Eğitim çalıştırılmadı.
- Lisans seçimi ve genel CI ayrı işlerdir.

## Bu teslimde uygulanmış kaynak

`operating-mode-config.v1`: model sınırlı ModeConfig önerir; güvenilir host
mevcut compiler ile aday üretir. Mod toleransı, NN tahmini, sensör residual'ı,
OMR ve mod/SOM uzaklığı ayrıdır. Aynı Director/bütçe/Scorer/Referee/scheduler
kullanılır; yeni yürütücü veya GPU authority yoktur. UI'de sıfır-token grid
ve bütçeli yerel agent seçenekleri ayrıdır. Canlı ana servisler kendiliğinden
restart edilmedi; kaynak/build teslimi aktif worker kabulü değildir.

Responsive model observer immutable owner/generation/invocation/execution
SHA'yı SQL'den salt okunur kontrol eder. Kuyruk, startup ve inference boyunca
stop/revoke görülürse kendi model çağrısı bırakılır; cleanup kanıtı olmadan
tahsis bırakılmaz ve mevcut quarantine korunur. Drain sırasında iptal
observer'ı çalışmaz. SQL kapanış guard'ları gevşetilmedi.

Authenticated `/v1/aos-capability/{suite_id}` mevcut AOS principal resolver
ve trusted registry pinlerini bildirir; GPU release alanı daima false.
Native launcher ve joint Lab callback halen private review adaylarıdır;
AOS kalan-total-deadline desteği/onaylı enabled config gelmeden kurulmaz.
[Source63 aktarımı](104-aos-source63-runtime-handoff.md).

## Kaynak teslim kapısı

Son zorunlu kapı2199 passed/7 skipped/120 GPU-live deselected; Ruff,
Pylint, Bandit, pytest, strict mypy, wheel build ve wheel import yedi exit0.
Frontend TypeScript/build exit0; yeni asset index-Bl7Qiyc4.js mevcut
console tarafından HTTP200 ile sunuluyor. Console/API yeniden başlatılmadı;
model başlatma mevcut ana konsolda kapalı, bu teslim açmadı.

24 implementasyon/test dosyası path→SHA manifesti SHA256:
`c3ed0fed31f7bf1a46c158aa7a4646cb69c5f625b4694f6ba53efe78b3277b8f`.
Private manifest ve kapı logu Scientist-owned runtime dizininde; yeni
koşunun admission-time kaynaklarıyla bu teslim karıştırılmaz. İlk gate'deki
assert ve üç eski fixture uyumsuzluğu düzeltildi; son kapı kaynak sabitken
çalıştırıldı. Native AOS, GPU stop/toparlanma ve structured model kabulü
bu CPU kapısında çalıştırılmadı. Push/merge/external deploy yapılmadı.
