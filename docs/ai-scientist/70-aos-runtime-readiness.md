# 70 — AOS runtime hazırlığı: kaynak mevcut, ortak kabul bekliyor

30 Eylül 2026. Bu not salt okunur kaynak incelemesidir; AOS import, test,
servis, SQL veya GPU çalıştırılmadı. AOS HEAD `ed6e857b0e61e9c19c8ba63933e2cc9f318fe444` çalışma ağacı
tracked değişiklikler ve untracked adaptörler içeriyor. Aşağıdaki seçili dosya
hash'leri inceleme başında/sonunda aynıydı; temiz commit, tam proje fingerprint'i
veya çalışan deployment kanıtı değildir. Scientist referansı `31cd67dbcebb096e4e68e478470acbc07c74e62e`.

## Mevcut kaynak

- Beklenen Decider/Bonsai broker worker yolları ve ortak broker runtime mevcut.
  `serve_desktop.main` artık `--engine scientist` ve açık mutlak
  `--scientist-broker-socket` ile host composition sağlar. Onaylı runtime provider
  yoksa başlangıcı reddeder. Eski `--shared-gpu-turns` / `lab-external` hook'ları
  bu yeni adaptörün zorunlu sözleşmesi değildir.
- `aos-scientist-runtime.v1` ortak öneridir; mevcut infer frame'in sürümü integer
  `1`dir. Frame request_id, sabit profile_id, deployment_digest ve payload taşır;
  istemci owner, lease veya fencing token seçmez. Scientist broker SO_PEERCRED ve
  doğrulanmış service generation kullanır; admission ve teslim öncesi generation
  tekrar doğrulanır (`lab/llm/aos_gpu_broker.py`, `serve_connection`).
- Scientist tek GPU allocation otoritesidir. Mevcut `gpu_scheduler.py`
  submit/try_acquire/cancel_queued/release/recover_quarantined yolları principal,
  owner/generation ve fencing kontrol eder. `aos_gpu_executor.py` aynı request ve
  digest'in durable replay'ini principal generation'a bağlar; acquire sonrası
  bounded model turn, exact unit/cgroup/GPU drain ve release yürütür. Başarısız
  drain queue'yu fenced/quarantined bırakır. Bunların kaynakta bulunması bu
  güncel AOS çiftinin gerçek GPU kabulü değildir.
- AOS Lab HTTP journal'ı effect öncesi intent'i kalıcılaştırır; lost ACK / sonuç
  kaydı hatası yeni işi engeller. Yerel async cancellation ve shutdown exact
  owned worker'ı bounded bekler. Local HTTP kapanması, stop ACK, idle veya terminal
  rapor GPU drain/release kanıtı değildir.

## Ortak teyit için aktarılacak maddeler

| Konu | Mevcut / açık durum |
| --- | --- |
| Capability ve version | Default-denied host verifier seam mevcut; iki tarafça doğrulanmış capability/version provider ve admitted kaynak/deployment çifti bekliyor. Yeni endpoint varsayılmıyor. |
| Principal / owner | Scientist authenticated service-generation kontrolü mevcut; gerçek AOS caller unit/principal ve deployment/profile digest eşlemesi ortak teyit bekliyor. |
| Acquire | Scientist scheduler tek otorite; AOS'ta ikinci GPU allocator kurulmaz. Gerçek sınırlı/fair turn devri bu çift için ölçülmedi. |
| Cancel / status | Lab HTTP stop/status ve yerel caller cancellation mevcut. Infer wire yalnız `infer`; ayrı ortak GPU cancel/status kontrol transport'u kabul edilmedi. Disconnect uzak turn'ün iptal veya release kanıtı değildir. |
| Drain / release | Scientist trusted exact-runtime doğrulaması mevcut. Dış adaptörün bu kanıtı hangi doğrulanmış kontrol yüzeyiyle alacağı ve fiziksel GPU kabulü açık. |
| Timeout / reconciliation | Bounded timeout ve durable uncertainty fences mevcut. Lost ACK, restart ve quarantined turn için ortak trusted reconciliation politikası/transport'u teyit bekliyor; kör retry/reset yok. |

Sonraki adım: bu kaynak fingerprint'ini AOS oturumuna iletip ortak version,
capability/principal ve cancellation/reconciliation/drain kanıtı sözleşmesini
teyit etmek. Ardından Scientist oturumu rezervasyon ve kullanıcı işi kontrolü
altında tek gerçek birlikte çalışma testini yürütür. Bu not runtime admission,
AOS/GPU kabulü veya native stop deneyinin tamamlandığını ilan etmez.

## Seçili AOS dosya kimlikleri

| Dosya | Git durumu | SHA-256 |
| --- | --- | --- |
| `scripts/serve_desktop.py` | tracked | `262466a66df0a8b3980710d786cdaec63f69000f205eaa5d3a86b2bf049c07e7` |
| `services/broker_runtime.py` | untracked | `3cfe4cf576eb4881fb0306ed313b14ba0cc5e69b152609ecfc92c3d14b91f207` |
| `services/decider/broker_worker.py` | untracked | `af04fab479d9d5326fd5456dd1592c9a444ee478831b50ef9070ee0b73d4cb8a` |
| `services/bonsai/broker_worker.py` | untracked | `69caf47c1beee4cae9c6c3170c0107acd8d8b80f45b60b5ba72d313ecbc2f528` |
| `src/aos/scientist_protocol.py` | untracked | `ef522f8759e7fb00b87a5b1a1ed7e922d5f6a7d1b0f1095dbcf38de68d3b5c60` |
| `src/aos/scientist_transport.py` | untracked | `805f9da8bde7fcba9936f8e9f1b5c13f09abaa2d83fad2e45051ae854439d252` |
| `src/aos/scientist_lab_journal.py` | untracked | `1573a4180815fe4bd90f31efe8e36915bc26a9cd3e6c4a474d4aaeb42751d79c` |
| `src/aos/scientist_lab_service.py` | untracked | `974cd098fb037b691a1d0bf232514fa25d0c94889585672417a6969588c76e9d` |

AOS'un kendi güncel açıklaması: `docs/SCIENTIST_HANDOFF.md` ve
`docs/SCIENTIST_RUNTIME_INTEGRATION.md` (aynı kaynak gözleminde hash'lendi).
Scientist kapsam ayrımı: [66](66-attempted-proposal-stop-candidate.md).
