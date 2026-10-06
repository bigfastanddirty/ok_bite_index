import unittest
from datetime import datetime, timezone
from test_accuracy import functions, ROOT


class TacticsAccuracy(unittest.TestCase):
    def setUp(self):
        self.ns = functions(ROOT / 'web/app.py')
        import sys
        sys.path.insert(0, str(ROOT / 'web'))
        import astronomy
        if hasattr(astronomy, 'solar_context'):
            self.ns['solar_context'] = astronomy.solar_context
        self.base = dict(ranked_species=['Bass, Largemouth'], water_temp_f=74,
                         diff_from_normal_ft=-1.18, elevation_delta_24h=-.05,
                         inflow_cfs=0, release_cfs=0, pressure_delta_hpa=-1,
                         wind_speed_mph=5, cloud_cover_pct=50, precipitation_in=0)

    def tactic(self, **changes):
        return self.ns['build_recommended_tactic'](**(self.base | changes), dt_val=datetime(2026,10,7,8,tzinfo=timezone.utc))

    def strategy(self, **changes):
        return self.ns['build_tactical_strategy'](**(self.base | changes),seasonal_phase='Fall Forage Push', solunar_window='NONE')

    def test_low_pool_does_not_prescribe_flooded_shoreline(self):
        self.assertNotIn('flooded shoreline', self.tactic().lower())

    def test_high_falling_pool_has_consistent_priority(self):
        for text in (self.tactic(diff_from_normal_ft=2,elevation_delta_24h=-.5),self.strategy(diff_from_normal_ft=2,elevation_delta_24h=-.5)):
            self.assertTrue('falling' in text.lower() or 'water falls' in text.lower())

    def test_missing_pool_is_not_called_normal(self):
        text=self.strategy(diff_from_normal_ft=None,elevation_delta_24h=None)
        self.assertNotIn('near normal',text)
        self.assertIn('confirm',text.lower())

    def test_missing_trend_is_not_called_stable(self):
        text=self.strategy(diff_from_normal_ft=2,elevation_delta_24h=None)
        self.assertNotIn('stable',text)
        self.assertIn('confirm',text.lower())

    def test_unknown_inputs_do_not_earn_ranking_points(self):
        _,details=self.ns['rank_target_species'](['Crappie, White'],None,1)
        self.assertEqual(details[0]['score'],50)

    def test_measured_stability_and_zero_wind_still_earn_points(self):
        _,details=self.ns['rank_target_species'](['Crappie, White'],None,1,elevation_delta_24h=0,wind_speed_mph=0)
        self.assertEqual(details[0]['score'],63)

    def test_estimated_temperature_is_qualified(self):
        text=self.strategy(water_temp_f=84,water_temp_is_estimated=True)
        self.assertIn('confirm',text.lower())
        self.assertNotIn('84',text)

    def test_strategy_is_an_action_plan_without_telemetry_repetition(self):
        text=self.strategy(inflow_cfs=500,release_cfs=900)
        self.assertLessEqual(len(text.split()),75)
        for token in ['°F','CFS',' ft','Pool is','temperature is','Condition-based candidates']:
            self.assertNotIn(token,text)

    def test_release_only_advice_identifies_tailwater(self):
        text=self.strategy(inflow_cfs=0,release_cfs=900)
        self.assertIn('dam release',text)
        self.assertIn('tailwater',text)
        self.assertNotIn('Strong current',text)

    def test_pressure_does_not_force_faster_presentations(self):
        self.assertNotIn('faster moving',self.tactic(ranked_species=['Crappie, White']))

    def test_night_strategy_does_not_describe_current_sunlight(self):
        text=self.strategy(water_temp_f=84,cloud_cover_pct=0,dt_val=datetime(2026,10,7,8,tzinfo=timezone.utc),lat=35.6,lon=-97.4)
        self.assertIn('night',text.lower())
        self.assertNotIn('early shallow window followed',text)

    def test_missing_temperature_cannot_imply_spawn(self):
        self.assertNotIn('spawn',self.ns['calculate_spawn_phase'](None,4)['phase'].lower())

    def test_lake_phase_does_not_claim_verified_species_spawn(self):
        self.assertNotIn('Active Spawn',self.ns['calculate_spawn_phase'](70,4)['phase'])

    def test_unknown_flow_is_distinct_from_measured_zero(self):
        self.assertEqual(self.ns['classify_current'](None,None),'UNKNOWN')
        self.assertEqual(self.ns['classify_current'](0,0),'NONE')
        self.assertEqual(self.ns['classify_current'](float('nan'),float('inf')),'UNKNOWN')

    def test_unknown_flow_does_not_penalize_paddlefish(self):
        _,details=self.ns['rank_target_species'](['Paddlefish'],None,1)
        self.assertEqual(details[0]['score'],50)


if __name__=='__main__': unittest.main()
