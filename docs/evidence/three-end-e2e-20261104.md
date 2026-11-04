# 三端 e2e 报告（real-catalog-20240101）

- 判定：**PASS**
- 时间：2026-11-04T14:57:27Z → 2026-11-04T14:57:33Z
- 链路：ResearchDataHub(CDS) → DBAS(catalog) → Hydroweave(model)

| 步骤 | 判定 | 说明 | 耗时(s) |
|---|---|---|---|
| ①+② RDH 采集 + DBAS 导入 | PASS | plan=plan-36f331c13fd1237f delivery=delivery-e448aa88fd0d4f35 files=1 imported=1 skipped=0 | 1.5 |
| ③ DBAS 解析元数据 | PASS | files=1 sha256_all_match=True catalog_variables=['ciwc', 'clwc', 'expver', 'latitude', 'longitude', 'number', 'pressure_level', 'valid_time'] | 0.2 |
| ④ Hydroweave 云水模型验证 | PASS | input_sha256_chain=True figures=5 run_dir=/Users/apple/Desktop/Hydroweave/data/analysis/cloud_water_strict/runs/real-catalog-20240101 | 3.4 |

## 校验链（SHA-256）

- `era5_cloud_water/canary_clwc_ciwc.nc`: d1b96325daa20adf342f60002a2d654ec41e9a835eee5a4acecea3ba6ab4cc39 （与交付清单一致=True）

## 图件

- /Users/apple/Desktop/Hydroweave/data/analysis/cloud_water_strict/runs/real-catalog-20240101/fig_identity_residual.png
- /Users/apple/Desktop/Hydroweave/data/analysis/cloud_water_strict/runs/real-catalog-20240101/fig_iwp_field.png
- /Users/apple/Desktop/Hydroweave/data/analysis/cloud_water_strict/runs/real-catalog-20240101/fig_lwp_field.png
- /Users/apple/Desktop/Hydroweave/data/analysis/cloud_water_strict/runs/real-catalog-20240101/fig_lwp_iwp_relation.png
- /Users/apple/Desktop/Hydroweave/data/analysis/cloud_water_strict/runs/real-catalog-20240101/fig_vertical_profile.png
